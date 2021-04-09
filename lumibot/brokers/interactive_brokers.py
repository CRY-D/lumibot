import asyncio
import logging
import traceback
from asyncio import CancelledError
from datetime import timezone
from dateutil import tz
import datetime

import ib_insync as ibi
import pandas_market_calendars as mcal

from lumibot.data_sources import InteractiveBrokersData
from lumibot.entities import Order, Position
from .broker import Broker

import nest_asyncio

nest_asyncio.apply()


class InteractiveBrokers(InteractiveBrokersData, Broker):
    """Inherit InteractiveBrokerData first and all the price market
    methods than inherits broker"""

    def __init__(self, config, connect_stream=False):
        # Calling init methods
        InteractiveBrokersData.__init__(
            self,
            config,
            max_workers=20,
            chunk_size=100,
        )
        Broker.__init__(self, name="interactive_brokers", connect_stream=connect_stream)

        self.api.connect(self.ip, self.socket_port, clientId=self.client_id)



    # =========Clock functions=====================

    def utc_to_local(self, utc_dt):
        return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=tz.tzlocal())

    def get_timestamp(self):
        """return current timestamp"""
        asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop()
        clock = self.api.reqCurrentTime()
        curr_time = clock.replace(tzinfo=timezone.utc).timestamp()
        return curr_time

    def market_hours(self, market="NASDAQ", close=True, date=None):
        mkt_cal = mcal.get_calendar(market)
        date = date if date is not None else datetime.datetime.now()
        trading_hours = mkt_cal.schedule(
            start_date=date, end_date=date + datetime.timedelta(weeks=1)
        ).head(1)

        market_open, market_close = trading_hours.values.tolist()[0]

        if close:
            return market_close
        else:
            return market_open

    def market_close_time(self):
        return self.utc_to_local(self.market_hours(close=True))

    def is_market_open(self):
        """return True if market is open else false"""
        open_time = self.utc_to_local(self.market_hours(close=False))
        close_time = self.utc_to_local(self.market_hours(close=True))

        current_time = datetime.datetime.now().astimezone(tz=tz.tzlocal())

        return (current_time >= open_time) and (close_time >= current_time)

    def get_time_to_open(self):
        """Return the remaining time for the market to open in seconds"""
        open_time = self.utc_to_local(self.market_hours(close=False))
        current_time = datetime.datetime.now().astimezone(tz=tz.tzlocal())
        if self.is_market_open():
            return None
        else:
            return open_time.timestamp() - current_time.timestamp()

    def get_time_to_close(self):
        """Return the remaining time for the market to close in seconds"""
        close_time = self.utc_to_local(self.market_hours(close=True))
        current_time = datetime.datetime.now().astimezone(tz=tz.tzlocal())
        if self.is_market_open():
            return close_time.timestamp() - current_time.timestamp()
        else:
            return None

    #
    # # =========Positions functions==================
    #
    # def _parse_broker_position(self, broker_position, strategy, orders=None):
    #     """parse a broker position representation
    #     into a position object"""
    #     symbol = broker_position.symbol
    #     quantity = broker_position.qty
    #     position = Position(strategy, symbol, quantity, orders=orders)
    #     return position

    def _pull_broker_position(self, symbol):
        """Given a symbol, get the broker representation
        of the corresponding symbol"""
        response = [
            pos for pos in self.api.positions() if pos.contract.localSymbol == symbol
        ]

        return response

    # @ibconnect
    def _pull_broker_positions(self):
        """Get the broker representation of all positions"""
        response = self.api.positions()
        return response

    # =======Orders and assets functions=========

    def _parse_broker_order(self, response, strategy):
        """parse a broker order representation
        to an order object"""
        order = Order(
            strategy,
            response.contract.localSymbol,
            response.order.totalQuantity,
            response.order.action,
            limit_price=response.order.lmtPrice,
            stop_price=response.order.adjustedStopPrice,
            time_in_force=response.order.tif,
        )
        if response.orderStatus.status:
            order._transmitted = True
        order.set_identifier(response.order.orderId)
        order.update_status(response.orderStatus.status)
        order.update_raw(response)
        return order

    def _pull_broker_order(self, order_id):
        """Get a broker order representation by its id"""
        pull_order = [
            order for order in self.api.openOrders() if order.orderId == order_id
        ]
        response = pull_order[0] if len(pull_order) > 0 else None
        return response

    def _pull_broker_open_orders(self):
        """Get the broker open orders"""
        orders = self.api.openOrders()
        return orders

    def _flatten_order(self, order):  # todo ask about this.
        """Some submitted orders may triggers other orders.
        _flatten_order returns a list containing the main order
        and all the derived ones"""
        # orders = [order]
        # if order._raw.legs:
        #     strategy = order.strategy
        #     for json_sub_order in order._raw.legs:
        #         sub_order = self._parse_broker_order(json_sub_order, strategy)
        #         orders.append(sub_order)

        return [order]  # todo made iterable for now.

    def submit_order(self, order):
        """Submit an order for an asset"""
        kwargs = {
            "type": order.type,
            "order_class": order.order_class,
            "time_in_force": order.time_in_force,
            "limit_price": order.limit_price,
        }

        # Remove items with None values
        kwargs = {k: v for k, v in kwargs.items() if v}

        if order.stop_price:
            kwargs["stop_loss"] = {"stop_price": order.stop_price}

        try:
            contract = ibi.Stock(order.symbol, "SMART", "USD")
            try:
                self.api.qualifyContracts(contract)
            except Exception as e:
                print(f"{e.message} {e.args}")

            if order.type == "market":
                ib_order = ibi.MarketOrder(
                    action=order.side, totalQuantity=order.quantity
                )
            elif order.type == "limit":
                ib_order = ibi.LimitOrder(
                    action=order.side,
                    totalQuantity=order.quantity,
                    lmtPrice=order.limit_price,
                )
            elif order.type == "stop_limit":
                ib_order = ibi.StopLimitOrder(
                    action=order.side,
                    totalQuantity=order.quantity,
                    lmtPrice=order.limit_price,
                    stopPrice=order.stop_price,
                )

            response = self.api.placeOrder(contract, ib_order)
            ib_order = self._parse_broker_order(response, order.strategy)
        except Exception as e:
            # order.set_error(e)
            logging.info(
                "%r did not go through. The following error occured: %s" % (order, e)
            )

        # return trade
        return ib_order

    def cancel_order(self, order_id):
        """Cancel an order"""
        order = self._pull_broker_order(order_id)
        print(order)
        if order:
            self.api.cancelOrder(order)

    # =========Market functions=======================

    def get_tradable_assets(self, easy_to_borrow=None, filter_func=None):
        """Get the list of all tradable assets from the market"""
        assets = self.api.list_assets()
        result = []
        for asset in assets:
            is_valid = asset.tradable
            if easy_to_borrow is not None and isinstance(easy_to_borrow, bool):
                is_valid = is_valid & (easy_to_borrow == asset.easy_to_borrow)
            if filter_func is not None:
                filter_test = filter_func(asset.symbol)
                is_valid = is_valid & filter_test

            if is_valid:
                result.append(asset.symbol)

        return result

    def _close_connection(self):
        self.api.disconnect()

    #
    # # =======Stream functions=========
    #
    # def _get_stream_object(self):
    #     """get the broker stream connection"""
    #     # stream = tradeapi.StreamConn(self.api_key, self.api_secret, self.endpoint)
    #     stream = self.get_connection()
    #     return stream
    #
    # def _register_stream_events(self):
    #     """Register the function on_trade_event
    #     to be executed on each trade_update event"""
    #     broker = self
    #
    #     @self.stream.on(r"^trade_updates$")
    #     async def on_trade_event(conn, channel, data):
    #         try:
    #             logged_order = data.order
    #             type_event = data.event
    #             identifier = logged_order.get("id")
    #             stored_order = broker.get_tracked_order(identifier)
    #             if stored_order is None:
    #                 logging.info(
    #                     "Untracker order %s was logged by broker %s"
    #                     % (identifier, broker.name)
    #                 )
    #                 return False
    #
    #             price = data.price if hasattr(data, "price") else None
    #             filled_quantity = data.qty if hasattr(data, "qty") else None
    #             broker._process_trade_event(
    #                 stored_order,
    #                 type_event,
    #                 price=price,
    #                 filled_quantity=filled_quantity,
    #             )
    #
    #             return True
    #         except:
    #             logging.error(traceback.format_exc())
    #
    # def _run_stream(self):
    #     """Overloading default alpaca_trade_api.STreamCOnnect().run()
    #     Run forever and block until exception is raised.
    #     initial_channels is the channels to start with.
    #     """
    #     loop = self.stream.loop
    #     should_renew = True  # should renew connection if it disconnects
    #     while should_renew:
    #         try:
    #             if loop.is_closed():
    #                 self.stream.loop = asyncio.new_event_loop()
    #                 loop = self.stream.loop
    #             loop.run_until_complete(self.stream.subscribe(["trade_updates"]))
    #             self._stream_established()
    #             loop.run_until_complete(self.stream.consume())
    #         except KeyboardInterrupt:
    #             logging.info("Exiting on Interrupt")
    #             should_renew = False
    #         except Exception as e:
    #             m = "consume cancelled" if isinstance(e, CancelledError) else e
    #             logging.error(f"error while consuming ws messages: {m}")
    #             if self.stream._debug:
    #                 logging.error(traceback.format_exc())
    #             loop.run_until_complete(self.stream.close(should_renew))
    #             if loop.is_running():
    #                 loop.close()
