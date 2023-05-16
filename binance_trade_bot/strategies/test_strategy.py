from collections import defaultdict#, deque
from colorama import Fore, Style
#import math
import random
import sys
import talib
import numpy
import decimal
import statistics as st
from datetime import datetime, timedelta
from scipy.interpolate import LSQUnivariateSpline

from sqlalchemy.orm import Session#, aliased
#from sqlalchemy.sql.expression import and_

from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.database import Pair #, Coin, CoinValue


class Strategy(AutoTrader):
    def initialize(self):

        if self.config.ACCEPT_LOSSES != True:
            self.logger.error("You need to accept losses by setting accept_losses=true in the user.cfg or setting the enviroment variable ACCEPT_LOSSES to true in order to use this strategy!")
            raise Exception()

        super().initialize()
        self.initialize_current_coin()
        self.rsi_coin = ""
        self.calcval = int(self.config.RSI_LENGTH)
        self.auto_weight = int(self.config.RATIO_ADJUST_WEIGHT)
        self.reinit_threshold = self.manager.now().replace(second=0, microsecond=0)
        self.reinit_rsi = self.manager.now().replace(second=0, microsecond=0)
        self.reinit_idle = self.manager.now().replace(second=0, microsecond=0) + timedelta(hours=int(self.config.MAX_IDLE_HOURS))
        self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)#int(self.config.RSI_CANDLE_TYPE))
        self.d = 3
        self.v = 3
        self.macd = False
        self.strikes = 0
        self.active_threshold = 0
        self.dir_threshold = 0
        self.Res_high = 0
        self.Res_mid = 0
        self.Res_low = 0
        self.Res_float = 0
        self.tema = 1
        self.rv_tema = 1
        self.vector = []
        self.volume = []
        self.volume_sma = []
        self.opens = []
        self.highs = []
        self.lows = []
        self.equi = False
        self.fair_price = 0
        self.next_price = 0
        self.sar = 0
        self.from_coin_price = 0
        self.to_coin_price = 0
        self.from_coin_direction = 0
        self.to_coin_direction = 0
        self.reverse_price_history = [0]
        self.rsi_price_history = [0]
        #self.panicked = self.check_panic()
        self.jumpable_coins = 0
        self.pre_rsi = 0
        self.rv_pre_rsi = 0
        self.rv_rsi = 0
        self.best_pair = ""
        self.rsi = self.rsi_calc()
        self.logger.info(f"Ratio adjust weight: {self.config.RATIO_ADJUST_WEIGHT}")
        self.logger.info(f"RSI starting length: {self.config.RSI_LENGTH}")
        self.logger.info(f"RSI candle type: {self.config.RSI_CANDLE_TYPE}")

    def scout(self):
        #check if previous buy order failed. If so, bridge scout for a new coin.
        if self.failed_buy_order:
            self.bridge_scout()

        current_coin = self.db.get_current_coin()
        ratio_dict, prices = self._get_ratios(current_coin, self.from_coin_price)
        panic_pair = max(ratio_dict, key=ratio_dict.get)

        base_time: datetime = self.manager.now()
        allowed_idle_time = self.reinit_threshold
        allowed_rsi_time = self.reinit_rsi

        if self.macd:
            self.from_coin_price = self.manager.get_buy_price(current_coin + self.config.BRIDGE)

        else:
            self.from_coin_price = self.manager.get_sell_price(current_coin + self.config.BRIDGE)

        if self.from_coin_price is None:
            self.logger.info("Skipping scouting... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        if base_time >= allowed_idle_time:
            print("")
            self.auto_weight = max(1, self.auto_weight + self.jumpable_coins - 1)
            if self.macd and self.from_coin_price >= self.active_threshold:
                self.active_threshold = self.active_threshold * 1.002**(1/self.calcval)
            elif not self.macd and self.from_coin_price <= self.active_threshold:
                self.active_threshold = self.active_threshold * (2 - 1.002**(1/self.calcval))
            self.re_initialize_trade_thresholds()
            self.reinit_threshold = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)

        if base_time >= allowed_rsi_time:
            self.rsi_calc()
            
            if self.macd:
                if self.from_coin_price > self.Res_high > self.active_threshold:
                    self.active_threshold = self.Res_high

                if self.from_coin_price > self.Res_mid > self.active_threshold:
                    self.active_threshold = self.Res_mid

                if self.from_coin_price > self.Res_low > self.active_threshold:
                    self.active_threshold = self.Res_low
                
            else:    
                if self.from_coin_price < self.Res_low < self.active_threshold:
                    self.active_threshold = self.Res_low

                if self.from_coin_price < self.Res_mid < self.active_threshold:
                    self.active_threshold = self.Res_mid

                if self.from_coin_price < self.Res_high < self.active_threshold:
                    self.active_threshold = self.Res_high
                    
            self.reinit_rsi = self.manager.now().replace(second=0, microsecond=0) + timedelta(seconds=1)

        """
        Scout for potential jumps from the current coin to another coin
        """
        # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot has
        # stopped. Not logging though to reduce log size.
        print(
            f"{self.manager.now().strftime('%Y-%m-%d %H:%M:%S')} - " ,
            #f"{Fore.CYAN}Holding{Style.RESET_ALL} " if not self.panicked else f"{Fore.CYAN}Awaiting{Style.RESET_ALL} ",
            f"{Fore.CYAN}Pivot{Style.RESET_ALL} " if self.equi else f"{Fore.CYAN}Moving{Style.RESET_ALL} ",
            f"Threshold: {Fore.CYAN}{round(self.from_coin_direction - self.dir_threshold, 3)}%{Style.RESET_ALL} " if self.dir_threshold != 0 else "",
            f"Bottom: {Fore.CYAN}{round(self.active_threshold, self.d)}{Style.RESET_ALL} " if not self.macd else f"Top: {Fore.CYAN}{round(self.active_threshold, self.d)}{Style.RESET_ALL} ",
            f"CalcVal: {Fore.CYAN}{self.calcval}{Style.RESET_ALL} ",
            f"Current coin: {Fore.CYAN}{current_coin}{Style.RESET_ALL} with RSI: {Fore.CYAN}{round(self.rv_rsi, 1)}{Style.RESET_ALL} price direction: {Fore.CYAN}{round(self.from_coin_direction, 1)}%{Style.RESET_ALL} ",
            f"rel. Volume: {Fore.CYAN}{round(self.volume[-1]/self.volume_sma, 2)}{Style.RESET_ALL} ",
            f"C: {Fore.MAGENTA}{round(self.Res_float, self.d)}{Style.RESET_ALL} FP: {Fore.MAGENTA}{round(self.fair_price, self.d)}{Style.RESET_ALL} NP: {Fore.MAGENTA}{round(self.next_price, self.d)}{Style.RESET_ALL} " if not self.macd else f"C: {Fore.GREEN}{round(self.Res_float, self.d)}{Style.RESET_ALL} FP: {Fore.GREEN}{round(self.fair_price, self.d)}{Style.RESET_ALL} NP: {Fore.GREEN}{round(self.next_price, self.d)}{Style.RESET_ALL} ",
            #f"L: {Fore.MAGENTA}{round(self.Res_low, self.d)}{Style.RESET_ALL} M: {Fore.MAGENTA}{round(self.Res_mid, self.d)}{Style.RESET_ALL} H: {Fore.MAGENTA}{round(self.Res_high, self.d)}{Style.RESET_ALL} C: {Fore.MAGENTA}{round(self.Res_float, self.d)}{Style.RESET_ALL} ",
            f"Next coin: {Fore.YELLOW}{self.rsi_coin}{Style.RESET_ALL} with RSI: {Fore.YELLOW}{round(self.rsi, 1)}{Style.RESET_ALL} price direction: {Fore.YELLOW}{round(self.to_coin_direction, 1)}%{Style.RESET_ALL} " if self.rsi else "",
            end='\r',
        )



        if self.rsi:
            if self.from_coin_direction <= self.to_coin_direction >= 0 and (self.pre_rsi < self.rsi <= 30 or 50 <= self.pre_rsi < self.rsi) or self.rsi < 20:
                print("")
                self.logger.info(f"Will be jumping from {current_coin} to {self.best_pair.to_coin_id}")
                self.transaction_through_bridge(self.best_pair, round(max(self.from_coin_price, self.rv_tema), self.d), round(min(self.to_coin_price, self.tema), self.v))
                self.auto_weight = int(self.config.RATIO_ADJUST_WEIGHT)
                #self.panicked = False
                self.active_threshold = self.to_coin_price * 1.002
                self.dir_threshold = 0
                self.equi = False
                self.fair_price = 0
                self.strikes = 0
                self.reinit_idle = self.manager.now().replace(second=0, microsecond=0) + timedelta(hours=int(self.config.MAX_IDLE_HOURS))
                self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)#int(self.config.RSI_CANDLE_TYPE))

                
        buy_balance = self.manager.get_currency_balance(self.config.BRIDGE) #(panic_pair.from_coin.symbol)
        can_buy = False
        
        if buy_balance and buy_balance >= self.manager.get_min_notional(panic_pair.from_coin.symbol, self.config.BRIDGE.symbol):
            can_buy = True
        
        sell_balance = self.manager.get_currency_balance(panic_pair.from_coin.symbol)
        can_sell = False
        if sell_balance and sell_balance * self.from_coin_price >= self.manager.get_min_notional(panic_pair.from_coin.symbol, self.config.BRIDGE.symbol):
            can_sell = True
            
        if not self.macd and base_time >= self.panic_time and can_buy:
            n = min(len(self.reverse_price_history), self.calcval)
            stdev = st.stdev(numpy.array(self.reverse_price_history[-n:]))
            self.dir_threshold = stdev / self.rv_tema * -100

            self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(seconds=1)

            if (self.rv_pre_rsi > self.rv_rsi and ((self.from_coin_direction < 0 and self.from_coin_price < self.active_threshold) or self.volume[-1] / self.volume_sma >= 1.5) or self.from_coin_direction < self.dir_threshold) or self.from_coin_price < self.active_threshold < self.next_price and self.equi or self.rv_rsi < 20 or min(self.vector[:-2]) >= self.vector[-1] or self.strikes >= self.calcval:
                if self.strikes >= self.calcval:
                    print("")
                    self.logger.info("!!! Strike out buy !!!")
                    self.active_threshold = self.from_coin_price
                    
                elif self.rv_rsi < 20 or min(self.vector[:-2]) >= self.vector[-1] or self.from_coin_price < self.active_threshold < self.next_price and self.equi:
                    print("")
                    self.logger.info("!!! Target buy !!!")
                    self.from_coin_price = round(min(self.from_coin_price, self.active_threshold), self.d)

                elif (self.from_coin_direction < self.dir_threshold and (self.rv_rsi > 50 or self.sar < self.Res_mid)) or (self.volume[-1] / self.volume_sma >= 1.5 and self.vector[-1] < 0):
                    print("")
                    self.logger.info("!!! FOMO buy !!!")
                    self.active_threshold = self.rv_tema
                    self.from_coin_price = round(self.rv_tema, self.d)

                else:
                    print("")
                    self.logger.info("!!! Buying low !!!")
                    self.from_coin_price = round(min(self.rv_tema, self.active_threshold, self.next_price), self.d)

                #self.panicked = True

                if self.manager.buy_part(panic_pair.from_coin, self.config.BRIDGE, self.from_coin_price) is None:
                    self.logger.info("Couldn't buy, going back to scouting mode...")
                    #self.panicked = False
                    self.strikes += 1
                    self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
                    #self.active_threshold = 0

                else:
                    self.active_threshold = min(self.from_coin_price * 0.998, self.from_coin_price - stdev) #max(self.reverse_price_history) * 3
                    self.dir_threshold = 0
                    self.equi = False
                    self.fair_price = 0
                    self.strikes = 0
                    self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1) #int(self.config.RSI_CANDLE_TYPE))


        elif self.macd and base_time >= self.panic_time and can_sell:
            #m = max(2 - (1+self.win/balance)**(1/(self.jumps)), 2 - 2**(1/(self.jumps)))-0.001
            n = min(len(self.reverse_price_history), self.calcval)
            stdev = st.stdev(numpy.array(self.reverse_price_history[-n:]))# * 0.73313783
            self.dir_threshold = stdev / self.rv_tema * 100

            self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(seconds=1)

            if self.macd and (self.rv_pre_rsi < self.rv_rsi and ((self.from_coin_direction > 0 and self.from_coin_price > self.active_threshold) or self.volume[-1] / self.volume_sma >= 1.5) or self.from_coin_direction > self.dir_threshold) or self.from_coin_price > self.active_threshold > self.next_price and self.equi or self.rv_rsi > 80 or max(self.vector[:-2]) <= self.vector[-1] or self.strikes >= self.calcval:
                
                if self.strikes >= self.calcval:
                    print("")
                    self.logger.info("!!! Strike out sell !!!")
                    self.active_threshold = self.from_coin_price
                
                elif self.rv_rsi > 80 or max(self.vector[:-2]) <= self.vector[-1] or self.from_coin_price > self.active_threshold > self.next_price and self.equi:
                    print("")
                    self.logger.info("!!! Target sell !!!")
                    self.from_coin_price = round(min(self.from_coin_price, self.active_threshold), self.d)

                elif (self.from_coin_direction > self.dir_threshold and (self.rv_rsi < 50 or self.sar > self.Res_mid)) or (self.volume[-1] / self.volume_sma >= 1.5 and self.vector[-1] > 0):
                    print("")
                    self.logger.info("!!! Panic sell !!!")
                    self.active_threshold = self.rv_tema
                    self.from_coin_price = round(self.rv_tema, self.d)

                else:
                    print("")
                    self.logger.info("!!! Selling high !!!")
                    self.from_coin_price = round(min(self.rv_tema, self.active_threshold, self.next_price), self.d)

                #self.panicked = False

                if self.manager.sell_part(panic_pair.from_coin, self.config.BRIDGE, self.from_coin_price) is None:
                    self.logger.info("Couldn't sell, going back to scouting mode...")
                    #self.panicked = True
                    self.strikes += 1
                    self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
                    #self.active_threshold = max(self.reverse_price_history) * 3

                else:
                    self.active_threshold = max(self.from_coin_price * 1.002, self.from_coin_price + stdev)
                    self.dir_threshold = 0
                    self.equi = False
                    self.fair_price = 0
                    self.strikes = 0
                    self.panic_time = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1) #int(self.config.RSI_CANDLE_TYPE))


    def bridge_scout(self):
        current_coin = self.db.get_current_coin()
        if self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            # Only scout if we don't have enough of the current coin
            return
        new_coin = super().bridge_scout()
        if new_coin is not None:
            self.db.set_current_coin(new_coin)

    def initialize_current_coin(self):
        """
        Decide what is the current coin, and set it up in the DB.
        """
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit("***\nERROR!\nSince there is no backup file, a proper coin name must be provided at init\n***")
            self.db.set_current_coin(current_coin_symbol)

            # if we don't have a configuration, we selected a coin at random... Buy it so we can start trading.
            if self.config.CURRENT_COIN_SYMBOL == "":
                current_coin = self.db.get_current_coin()
                self.logger.info(f"Purchasing {current_coin} to begin trading")
                self.manager.buy_alt(
                    current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                )
                self.logger.info("Ready to start trading")
            else:
                current_balance = self.manager.get_currency_balance(current_coin_symbol)
                sell_price = self.manager.get_sell_price(current_coin_symbol + self.config.BRIDGE.symbol)
                if current_balance is not None and current_balance * sell_price < self.manager.get_min_notional(current_coin_symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Purchasing {current_coin_symbol} to begin trading")
                    current_coin = self.db.get_current_coin()
                    self.manager.buy_alt(
                        current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                    )
                    self.logger.info("Ready to start trading")

    def re_initialize_trade_thresholds(self):
        """
        Re-initialize all the thresholds ( hard reset - as deleting db )
        """
        #updates all ratios
        #print('************INITIALIZING RATIOS**********')
        session: Session
        with self.db.db_session() as session:
            #c1 = aliased(Coin)
            #c2 = aliased(Coin)
            for pair in session.query(Pair).all():
                if not pair.from_coin.enabled or not pair.to_coin.enabled:
                    continue
                #self.logger.debug(f"Initializing {pair.from_coin} vs {pair.to_coin}", False)

                from_coin_price = self.manager.get_sell_price(pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    # self.logger.debug(
                    #     "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE),
                    #     False
                    # )
                    continue

                to_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None:
                    # self.logger.debug(
                    #     "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE),
                    #     False
                    # )
                    continue

                pair.ratio = (pair.ratio *self.auto_weight + from_coin_price / to_coin_price)  / (self.auto_weight + 1)

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        session: Session
        with self.db.db_session() as session:
            pairs = session.query(Pair).filter(Pair.ratio.is_(None)).all()
            grouped_pairs = defaultdict(list)
            for pair in pairs:
                if pair.from_coin.enabled and pair.to_coin.enabled:
                    grouped_pairs[pair.from_coin.symbol].append(pair)

            price_history = {}

            init_weight = self.config.RATIO_ADJUST_WEIGHT

            #Binance api allows retrieving max 1000 candles
            if init_weight > 500:
                init_weight = 500

            self.logger.info(f"Using last {init_weight} candles to initialize ratios")

            base_date = self.manager.now().replace(second=0, microsecond=0)
            start_date = base_date - timedelta(minutes=init_weight*2)
            end_date = base_date - timedelta(minutes=1)

            start_date_str = start_date.strftime('%Y-%m-%d %H:%M')
            end_date_str = end_date.strftime('%Y-%m-%d %H:%M')

            self.logger.info(f"Starting ratio init: Start Date: {start_date}, End Date {end_date}")
            for from_coin_symbol, group in grouped_pairs.items():

                if from_coin_symbol not in price_history.keys():
                    price_history[from_coin_symbol] = []
                    for result in  self.manager.binance_client.get_historical_klines(f"{from_coin_symbol}{self.config.BRIDGE_SYMBOL}", "1m", start_date_str, end_date_str, limit=1000):
                        price = float(result[4])
                        price_history[from_coin_symbol].append(price)

                for pair in group:
                    to_coin_symbol = pair.to_coin.symbol
                    if to_coin_symbol not in price_history.keys():
                        price_history[to_coin_symbol] = []
                        for result in self.manager.binance_client.get_historical_klines(f"{to_coin_symbol}{self.config.BRIDGE_SYMBOL}", "1m", start_date_str, end_date_str, limit=1000):
                           price = float(result[4])
                           price_history[to_coin_symbol].append(price)

                    if len(price_history[from_coin_symbol]) != init_weight*2:
                        self.logger.info(len(price_history[from_coin_symbol]))
                        self.logger.info(f"Skip initialization. Could not fetch last {init_weight * 2} prices for {from_coin_symbol}")
                        continue
                    if len(price_history[to_coin_symbol]) != init_weight*2:
                        self.logger.info(f"Skip initialization. Could not fetch last {init_weight * 2} prices for {to_coin_symbol}")
                        continue

                    sma_ratio = 0.0
                    for i in range(init_weight):
                        sma_ratio += price_history[from_coin_symbol][i] / price_history[to_coin_symbol][i]
                    sma_ratio = sma_ratio / init_weight

                    cumulative_ratio = sma_ratio
                    for i in range(init_weight, init_weight * 2):
                        cumulative_ratio = (cumulative_ratio * init_weight + price_history[from_coin_symbol][i] / price_history[to_coin_symbol][i]) / (init_weight + 1)

                    pair.ratio = cumulative_ratio

            self.logger.info("Finished ratio init...")


    def rsi_calc(self):
        """
        Calculate the RSI for the next best coin.
        """

        init_rsi_length = self.calcval
        rsi_type = self.config.RSI_CANDLE_TYPE
        rsi_string = str(self.config.RSI_CANDLE_TYPE) + 'm'

        #Binance api allows retrieving max 1000 candles
        if init_rsi_length > 20:
            init_rsi_length = 20

        init_rsi_delta = (init_rsi_length * 50 ) * rsi_type
        rsi_base_date = self.manager.now().replace(second=0, microsecond=0)
        rsi_start_date = rsi_base_date - timedelta(minutes=init_rsi_delta)
        rsi_end_date = rsi_base_date
        rsi_check_date = rsi_start_date + timedelta(minutes=self.config.RSI_CANDLE_TYPE*2)

        rsi_start_date_str = rsi_start_date.strftime('%Y-%m-%d %H:%M')
        rsi_end_date_str = rsi_end_date.strftime('%Y-%m-%d %H:%M')
        rsi_check_str = rsi_check_date.strftime('%Y-%m-%d %H:%M')

        current_coin = self.db.get_current_coin()
        current_coin_symbol = current_coin.symbol
        current_coin_price = self.manager.get_sell_price(current_coin + self.config.BRIDGE)

        ratio_dict, prices = self._get_ratios(current_coin, current_coin_price)
        ratio_dict = {k: v for k, v in ratio_dict.items() if v > 0}

        self.jumpable_coins = len(ratio_dict)
        self.d = abs(decimal.Decimal(str(self.reverse_price_history[-1])).as_tuple().exponent)
        self.v = abs(decimal.Decimal(str(self.rsi_price_history[-1])).as_tuple().exponent)

        for i in range(1, len(self.reverse_price_history)-1):
            di = abs(decimal.Decimal(str(self.reverse_price_history[i])).as_tuple().exponent)
            if di >= self.d:
                self.d = di
                if self.d == 1 and str(self.reverse_price_history[-1] % 1)[2] == '0':
                    self.d = 0

        for k in range(1, len(self.rsi_price_history)-1):
            vi = abs(decimal.Decimal(str(self.rsi_price_history[k])).as_tuple().exponent)
            if vi >= self.v:
                self.v = vi
                if self.v == 1 and str(self.rsi_price_history[-1] % 1)[2] == '0':
                    self.v = 0


        if ratio_dict:
            self.best_pair = max(ratio_dict, key=ratio_dict.get)
            to_coin_symbol = self.best_pair.to_coin_id
            check_prices = []

            for checks in self.manager.binance_client.get_historical_klines(f"{to_coin_symbol}{self.config.BRIDGE_SYMBOL}", rsi_string, rsi_start_date_str, rsi_check_str, limit=1):
                check_price = float(checks[4])
                check_prices.append(check_price)


            if self.db.get_coin(to_coin_symbol) == self.rsi_coin and self.rsi_price_history[0] == check_prices[0]:
                self.to_coin_price = self.manager.get_buy_price(self.rsi_coin + self.config.BRIDGE)
                self.rsi_price_history[-1] = self.to_coin_price
            else:
                self.rsi_coin = self.db.get_coin(to_coin_symbol)
                self.rsi_price_history = []

                for result in self.manager.binance_client.get_historical_klines(f"{to_coin_symbol}{self.config.BRIDGE_SYMBOL}", rsi_string, rsi_start_date_str, rsi_end_date_str, limit=1000):
                    rsi_price = float(result[4])
                    self.rsi_price_history.append(rsi_price)

                self.to_coin_price = self.manager.get_buy_price(self.rsi_coin + self.config.BRIDGE)
                self.rsi_price_history.append(self.to_coin_price)



            if len(self.rsi_price_history) >= init_rsi_length:
                np_closes = numpy.array(self.rsi_price_history)
                rsi = talib.RSI(np_closes, init_rsi_length)
                tema = talib.TEMA(np_closes, init_rsi_length)

                self.rsi = rsi[-1]
                self.pre_rsi = rsi[-2]
                self.tema = tema[-2]
                self.to_coin_direction = self.to_coin_price / self.tema * 100 - 100

        else:
            self.rsi = 0
            self.pre_rsi = 0
            self.tema = 1
            self.to_coin_price = 0
            self.best_pair = ""
            self.to_coin_direction = 0

        ExpPer = 2 * init_rsi_length - 1
        K = 2 / (ExpPer + 1)
        AUC = 1
        ADC = 1
        rev_prices = []

        for reverse in self.manager.binance_client.get_historical_klines(f"{current_coin_symbol}{self.config.BRIDGE_SYMBOL}", rsi_string, rsi_start_date_str, rsi_check_str, limit=init_rsi_length*5):
            rev_price = float(reverse[4])
            rev_prices.append(rev_price)

        if not self.reverse_price_history[0] == rev_prices[0]:
            self.reverse_price_history = []
            self.volume = []
            self.vector =[]
            self.highs = []
            self.lows = []
            self.opens = []
            for result in self.manager.binance_client.get_historical_klines(f"{current_coin_symbol}{self.config.BRIDGE_SYMBOL}", rsi_string, rsi_start_date_str, rsi_end_date_str, limit=init_rsi_length*5):
                opens = float(result[1])
                high = float(result[2])
                low = float(result[3])
                rsi_price = float(result[4])
                volume = float(result[5])
                vector = (rsi_price - float(result[1])) * volume
                self.reverse_price_history.append(rsi_price)
                self.volume.append(volume)
                self.vector.append(vector)
                self.highs.append(high)
                self.lows.append(low)
                self.opens.append(opens)

        else:

            for result in self.manager.binance_client.get_historical_klines(f"{current_coin_symbol}{self.config.BRIDGE_SYMBOL}", rsi_string, limit=1):
                opens = float(result[1])
                high = float(result[2])
                low = float(result[3])
                close = float(result[4])
                volume = float(result[5])
                vector = (close - float(result[1])) * volume
                self.volume[-1] = volume
                self.reverse_price_history[-1] = self.from_coin_price
                self.vector[-1] = vector
                self.highs[-1] = high
                self.lows[-1] = low
                self.opens[-1] = opens
                
        hist_d=[]
        if len(self.reverse_price_history) >= 26: #init_rsi_length:
            rv_closes = numpy.array(self.reverse_price_history)
            rv_rsi = talib.RSI(rv_closes, init_rsi_length)
            rv_tema = talib.TEMA(rv_closes, init_rsi_length)
            
            calc = self.config.RSI_LENGTH
            macd, macdsignal, macdhist = talib.MACD(rv_closes, fastperiod=int(round(self.calcval*12/calc,0)), slowperiod=int(round(self.calcval*26/calc,0)), signalperiod=int(round(self.calcval*9/calc,0)))
                
            if macd[-1] - macdsignal[-1] > macd[-2] - macdsignal[-2]:
                self.macd = True
            elif macd[-1] - macdsignal[-1] < macd[-2] - macdsignal[-2]:
                self.macd = False

            volume = numpy.array(self.volume)
            volume_sma = talib.SMA(volume, init_rsi_length)

            highs = numpy.array(self.highs)
            lows = numpy.array(self.lows)
            opens = numpy.array(self.opens)
            sar = talib.SAR(highs, lows, acceleration=0.02, maximum=20)

            comb = zip(self.reverse_price_history, self.reverse_price_history, self.highs, self.lows) #, self.opens)
            hlc = []
            for values in comb:
                hlc.append(sum(values) / 4)
                
            try:
                bins_a = int((max(self.highs) - min(self.lows)) / (st.stdev(numpy.array(self.reverse_price_history[-1-self.calcval:-2])))) + 1
            except:
                bins_a = self.config.RSI_LENGTH + 1
            
            count, bins = numpy.histogram(hlc, bins=bins_a)
            allocs = numpy.digitize(hlc, bins) - 1
            position_now = numpy.digitize(self.from_coin_price, bins) -1 #numpy.digitize(hlc[-1], bins) - 1

            hist = {}
            hist = {i: 0 for i in range(len(bins)+1)}

            for a,vol in zip(allocs, volume):
                if not a in hist:
                    hist[a] = bins[a] * vol
                else:
                    hist[a] += bins[a] * vol

            for i in hist:
                if hist[max(i-1, 0)] <= hist[max(i, 0)] >= hist[min(i+1, len(hist)-1)]:
                    hist_d.append(i)
                
            s = self.calcval
            ps_x = []
            ps_y = []
            ps_w = []
            for i in range(len(hlc)):
                if len(self.reverse_price_history[max(i-s,0):i]) >= 2:
                    weight = self.reverse_price_history[i]/(self.reverse_price_history[i]+st.stdev(numpy.array(self.reverse_price_history[max(i-s,0):i])))
                    if allocs[i] not in hist_d:
                        ps_w.append(weight)
                    else:
                        ps_w.append(1/weight)        
                else:
                    ps_w.append(1)
                ps_x.append(i)
                ps_y.append(hlc[i])
            
            ps_t = []
            ps_h = []
            for i in range(1, len(hlc)-1):
                if allocs[i] in hist_d:
                    if allocs[i] == allocs[i+1]:
                        ps_h.append(i)
                    elif ps_h:
                        ps_h.append(i)
                        ps_t.append(sum(ps_h)/len(ps_h))
                        ps_h = []
                    else:
                        ps_t.append(i)
            if ps_h:
                ps_t.append(sum(ps_h)/len(ps_h))
                ps_h = []
            
            spline = LSQUnivariateSpline(ps_x, ps_y, ps_t, w=ps_w, k=3)#, s=s)
            xx= numpy.linspace(len(hlc)-1, len(hlc), 10)
            yy=spline(xx)

            if hist[max(position_now-1, 0)] <= hist[max(position_now, 0)] >= hist[min(position_now+1,len(hist)-1)]:
                self.equi = True
            else:
                self.equi = False

            fair_price, max_value = max(hist.items(), key=lambda x: x[1])
            
            self.next_price = yy[-1]
            self.fair_price = bins[fair_price]
            self.rv_rsi = rv_rsi[-1]
            self.rv_pre_rsi = rv_rsi[-2]
            self.rv_tema = rv_tema[-2]
            self.from_coin_direction = self.from_coin_price / self.rv_tema * 100 - 100
            self.sar = sar[-1]
            self.volume_sma = volume_sma[-1]

        prev_close = self.reverse_price_history[0]
        for close in self.reverse_price_history[1:]:
            if close > prev_close:
                AUC = K * (close - prev_close) + (1 - K) * AUC
                ADC = (1 - K) * ADC

            else:
                AUC = (1 - K) * AUC
                ADC = K * (prev_close - close) + (1 - K) * ADC
            prev_close = close

        Val_high = (init_rsi_length - 1) * (ADC * 70/30 - AUC)
        Val_mid = (init_rsi_length - 1) * (ADC - AUC)
        Val_low = (init_rsi_length - 1) * (ADC * 30/70 - AUC)
        Val_float = (init_rsi_length - 1) * (ADC * self.rv_rsi / (100 - self.rv_rsi) - AUC)

        if Val_high >= 0:
            self.Res_high = self.reverse_price_history[-1] + Val_high
        else:
            self.Res_high = self.reverse_price_history[-1] + (Val_high * 30/70)

        self.Res_mid = self.reverse_price_history[-1] + Val_mid

        if Val_low >= 0:
            self.Res_low = self.reverse_price_history[-1] + Val_low
        else:
            self.Res_low = self.reverse_price_history[-1] + (Val_low * 70/30)

        if Val_float >= 0:
            self.Res_float = self.reverse_price_history[-1] + Val_float
        else:
            self.Res_float = self.reverse_price_history[-1] + (Val_float * (100/self.rv_rsi - 1))
        
        if self.calcval-len(hist_d) > 1:
            self.calcval = self.calcval - 1
        elif len(hist_d)-self.calcval > 1:
            self.calcval = self.calcval + 1
