# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import signal
import threading
import time

from web3 import Web3

from keeper import Logger, AsyncCallback, register_filter_thread, any_filter_thread_present, stop_all_filter_threads, \
    all_filter_threads_alive


class Web3Lifecycle:
    def __init__(self, web3: Web3, logger: Logger):
        self.web3 = web3
        self.logger = logger
        self.startup_function = None
        self.shutdown_function = None

        self.terminated_internally = False
        self.terminated_externally = False
        self.fatal_termination = False
        self._at_least_one_every = False
        self._last_block_time = None
        self._on_block_callback = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # self.logger.info(f"{self.executable_name()}")
        # self.logger.info(f"{'-' * len(self.executable_name())}")
        # on {self.chain},
        self.logger.info(f"Keeper connected to {self.web3.providers[0].endpoint_uri}")
        self.logger.info(f"Keeper operating as {self.web3.eth.defaultAccount}")
        self._check_account_unlocked()
        self._wait_for_init()
        # self.print_eth_balance()
        self.logger.info("Keeper started")
        if self.startup_function:
            self.startup_function(self)
        self._main_loop()
        self.logger.info("Shutting down the keeper")
        if any_filter_thread_present():
            self.logger.info("Waiting for all threads to terminate...")
            stop_all_filter_threads()
        if self._on_block_callback is not None:
            self.logger.info("Waiting for outstanding callback to terminate...")
            self._on_block_callback.wait()
        if self.shutdown_function:
            self.logger.info("Executing keeper shutdown logic...")
            self.shutdown_function(self)
            self.logger.info("Shutdown logic finished")
        self.logger.info("Keeper terminated")
        exit(10 if self.fatal_termination else 0)

    def _wait_for_init(self):
        # wait for the client to have at least one peer
        if self.web3.net.peerCount == 0:
            self.logger.info(f"Waiting for the node to have at least one peer...")
            while self.web3.net.peerCount == 0:
                time.sleep(0.25)

        # wait for the client to sync completely,
        # as we do not want to apply keeper logic to stale blocks
        if self.web3.eth.syncing:
            self.logger.info(f"Waiting for the node to sync...")
            while self.web3.eth.syncing:
                time.sleep(0.25)

    def _check_account_unlocked(self):
        try:
            self.web3.eth.sign(self.web3.eth.defaultAccount, "test")
        except:
            self.logger.fatal(f"Account {self.web3.eth.defaultAccount} is not unlocked")
            self.logger.fatal(f"Unlocking the account is necessary for the keeper to operate")
            exit(-1)

    def on_startup(self, callback):
        self.startup_function = callback

    def on_shutdown(self, callback):
        self.shutdown_function = callback

    # TODO should queue the callback and apply it only after keeper startup
    def on_block(self, callback):
        def new_block_callback(block_hash):
            self._last_block_time = datetime.datetime.now()
            block = self.web3.eth.getBlock(block_hash)
            block_number = block['number']
            if not self.web3.eth.syncing:
                max_block_number = self.web3.eth.blockNumber
                if block_number == max_block_number:
                    def on_start():
                        self.logger.debug(f"Processing block #{block_number} ({block_hash})")

                    def on_finish():
                        self.logger.debug(f"Finished processing block #{block_number} ({block_hash})")

                    if not self._on_block_callback.trigger(on_start, on_finish):
                        self.logger.info(f"Ignoring block #{block_number} ({block_hash}),"
                                         f" as previous callback is still running")
                else:
                    self.logger.info(f"Ignoring block #{block_number} ({block_hash}),"
                                     f" as there is already block #{max_block_number} available")
            else:
                self.logger.info(f"Ignoring block #{block_number} ({block_hash}), as the node is syncing")

        self._on_block_callback = AsyncCallback(callback)

        block_filter = self.web3.eth.filter('latest')
        block_filter.watch(new_block_callback)
        register_filter_thread(block_filter)

        self.logger.info("Watching for new blocks")

    # TODO should queue the every and apply it only after keeper startup
    def every(self, frequency_in_seconds: int, callback):
        def setup_timer(delay):
            timer = threading.Timer(delay, func)
            timer.daemon = True
            timer.start()

        def func():
            try:
                callback()
            except:
                setup_timer(frequency_in_seconds)
                raise
            setup_timer(frequency_in_seconds)

        setup_timer(1)
        self._at_least_one_every = True

    def sigint_sigterm_handler(self, sig, frame):
        if self.terminated_externally:
            self.logger.warning("Graceful keeper termination due to SIGINT/SIGTERM already in progress")
        else:
            self.logger.warning("Keeper received SIGINT/SIGTERM signal, will terminate gracefully")
            self.terminated_externally = True

    def _main_loop(self):
        # terminate gracefully on either SIGINT or SIGTERM
        signal.signal(signal.SIGINT, self.sigint_sigterm_handler)
        signal.signal(signal.SIGTERM, self.sigint_sigterm_handler)

        # in case at least one filter has been set up, we enter an infinite loop and let
        # the callbacks do the job. in case of no filters, we will not enter this loop
        # and the keeper will terminate soon after it started
        while any_filter_thread_present() or self._at_least_one_every:
            time.sleep(1)

            # if the keeper logic asked us to terminate, we do so
            if self.terminated_internally:
                self.logger.warning("Keeper logic asked for termination, the keeper will terminate")
                break

            # if SIGINT/SIGTERM asked us to terminate, we do so
            if self.terminated_externally:
                self.logger.warning("The keeper is terminating due do SIGINT/SIGTERM signal received")
                break

            # if any exception is raised in filter handling thread (could be an HTTP exception
            # while communicating with the node), web3.py does not retry and the filter becomes
            # dysfunctional i.e. no new callbacks will ever be fired. we detect it and terminate
            # the keeper so it can be restarted.
            if not all_filter_threads_alive():
                self.logger.fatal("One of filter threads is dead, the keeper will terminate")
                self.fatal_termination = True
                break

            # if we are watching for new blocks and no new block has been reported during
            # some time, we assume the watching filter died and terminate the keeper
            # so it can be restarted.
            #
            # this used to happen when the machine that has the node and the keeper running
            # was put to sleep and then woken up.
            #
            # TODO the same thing could possibly happen if we watch any event other than
            # TODO a new block. if that happens, we have no reliable way of detecting it now.
            if self._last_block_time and (datetime.datetime.now() - self._last_block_time).total_seconds() > 300:
                if not self.web3.eth.syncing:
                    self.logger.fatal("No new blocks received for 300 seconds, the keeper will terminate")
                    self.fatal_termination = True
                    break