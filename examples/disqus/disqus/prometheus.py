import uuid
import logging

from prometheus_client.twisted import MetricsResource
from prometheus_client import Counter, Summary, Gauge
from twisted.web.server import Site
from twisted.web.resource import Resource
from twisted.internet import reactor
from scrapy import signals

logger = logging.getLogger(__name__)

import datetime

from scrapy import signals

class CoreStats(object):

    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        o = cls(crawler.stats)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(o.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(o.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(o.response_received, signal=signals.response_received)
        return o

    def spider_opened(self, spider):
        self.stats.set_value('start_time', datetime.datetime.utcnow(), spider=spider)

    def spider_closed(self, spider, reason):
        self.stats.set_value('finish_time', datetime.datetime.utcnow(), spider=spider)
        self.stats.set_value('finish_reason', reason, spider=spider)

    def item_scraped(self, item, spider):
        self.stats.inc_value('item_scraped_count', spider=spider)

    def response_received(self, spider):
        self.stats.inc_value('response_received_count', spider=spider)

    def item_dropped(self, item, spider, exception):
        reason = exception.__class__.__name__
        self.stats.inc_value('item_dropped_count', spider=spider)
        self.stats.inc_value('item_dropped_reasons_count/%s' % reason, spider=spider)



import logging

from twisted.internet import task

from scrapy.exceptions import NotConfigured
from scrapy import signals

logger = logging.getLogger(__name__)


class LogStats(object):
    """Log basic scraping stats periodically"""

    def __init__(self, stats, interval=60.0):
        self.stats = stats
        self.interval = interval
        self.multiplier = 60.0 / self.interval
        self.task = None

    @classmethod
    def from_crawler(cls, crawler):
        interval = crawler.settings.getfloat('LOGSTATS_INTERVAL')
        if not interval:
            raise NotConfigured
        o = cls(crawler.stats, interval)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        return o

    def spider_opened(self, spider):
        self.pagesprev = 0
        self.itemsprev = 0

        self.task = task.LoopingCall(self.log, spider)
        self.task.start(self.interval)

    def log(self, spider):
        items = self.stats.get_value('item_scraped_count', 0)
        pages = self.stats.get_value('response_received_count', 0)
        irate = (items - self.itemsprev) * self.multiplier
        prate = (pages - self.pagesprev) * self.multiplier
        self.pagesprev, self.itemsprev = pages, items

        msg = ("Crawled %(pages)d pages (at %(pagerate)d pages/min), "
               "scraped %(items)d items (at %(itemrate)d items/min)")
        log_args = {'pages': pages, 'pagerate': prate,
                    'items': items, 'itemrate': irate}
        logger.info(msg, log_args, extra={'spider': spider})

    def spider_closed(self, spider, reason):
        if self.task and self.task.running:
            self.task.stop()




"""
MemoryDebugger extension

See documentation in docs/topics/extensions.rst
"""

import gc
import six

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.trackref import live_refs


class MemoryDebugger(object):

    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool('MEMDEBUG_ENABLED'):
            raise NotConfigured
        o = cls(crawler.stats)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        return o

    def spider_closed(self, spider, reason):
        gc.collect()
        self.stats.set_value('memdebug/gc_garbage_count', len(gc.garbage), spider=spider)
        for cls, wdict in six.iteritems(live_refs):
            if not wdict:
                continue
            self.stats.set_value('memdebug/live_refs/%s' % cls.__name__, len(wdict), spider=spider)




"""
MemoryUsage extension

See documentation in docs/topics/extensions.rst
"""
import sys
import socket
import logging
from pprint import pformat
from importlib import import_module

from twisted.internet import task

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.mail import MailSender
from scrapy.utils.engine import get_engine_status

logger = logging.getLogger(__name__)


class MemoryUsage(object):

    def __init__(self, crawler):
        if not crawler.settings.getbool('MEMUSAGE_ENABLED'):
            raise NotConfigured
        try:
            # stdlib's resource module is only available on unix platforms.
            self.resource = import_module('resource')
        except ImportError:
            raise NotConfigured

        self.crawler = crawler
        self.warned = False
        self.notify_mails = crawler.settings.getlist('MEMUSAGE_NOTIFY_MAIL')
        self.limit = crawler.settings.getint('MEMUSAGE_LIMIT_MB')*1024*1024
        self.warning = crawler.settings.getint('MEMUSAGE_WARNING_MB')*1024*1024
        self.check_interval = crawler.settings.getfloat('MEMUSAGE_CHECK_INTERVAL_SECONDS')
        self.mail = MailSender.from_settings(crawler.settings)
        crawler.signals.connect(self.engine_started, signal=signals.engine_started)
        crawler.signals.connect(self.engine_stopped, signal=signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def get_virtual_size(self):
        size = self.resource.getrusage(self.resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != 'darwin':
            # on Mac OS X ru_maxrss is in bytes, on Linux it is in KB
            size *= 1024
        return size

    def engine_started(self):
        self.crawler.stats.set_value('memusage/startup', self.get_virtual_size())
        self.tasks = []
        tsk = task.LoopingCall(self.update)
        self.tasks.append(tsk)
        tsk.start(self.check_interval, now=True)
        if self.limit:
            tsk = task.LoopingCall(self._check_limit)
            self.tasks.append(tsk)
            tsk.start(self.check_interval, now=True)
        if self.warning:
            tsk = task.LoopingCall(self._check_warning)
            self.tasks.append(tsk)
            tsk.start(self.check_interval, now=True)

    def engine_stopped(self):
        for tsk in self.tasks:
            if tsk.running:
                tsk.stop()

    def update(self):
        self.crawler.stats.max_value('memusage/max', self.get_virtual_size())

    def _check_limit(self):
        if self.get_virtual_size() > self.limit:
            self.crawler.stats.set_value('memusage/limit_reached', 1)
            mem = self.limit/1024/1024
            logger.error("Memory usage exceeded %(memusage)dM. Shutting down Scrapy...",
                         {'memusage': mem}, extra={'crawler': self.crawler})
            if self.notify_mails:
                subj = "%s terminated: memory usage exceeded %dM at %s" % \
                        (self.crawler.settings['BOT_NAME'], mem, socket.gethostname())
                self._send_report(self.notify_mails, subj)
                self.crawler.stats.set_value('memusage/limit_notified', 1)

            open_spiders = self.crawler.engine.open_spiders
            if open_spiders:
                for spider in open_spiders:
                    self.crawler.engine.close_spider(spider, 'memusage_exceeded')
            else:
                self.crawler.stop()

    def _check_warning(self):
        if self.warned: # warn only once
            return
        if self.get_virtual_size() > self.warning:
            self.crawler.stats.set_value('memusage/warning_reached', 1)
            mem = self.warning/1024/1024
            logger.warning("Memory usage reached %(memusage)dM",
                           {'memusage': mem}, extra={'crawler': self.crawler})
            if self.notify_mails:
                subj = "%s warning: memory usage reached %dM at %s" % \
                        (self.crawler.settings['BOT_NAME'], mem, socket.gethostname())
                self._send_report(self.notify_mails, subj)
                self.crawler.stats.set_value('memusage/warning_notified', 1)
            self.warned = True

    def _send_report(self, rcpts, subject):
        """send notification mail with some additional useful info"""
        stats = self.crawler.stats
        s = "Memory usage at engine startup : %dM\r\n" % (stats.get_value('memusage/startup')/1024/1024)
        s += "Maximum memory usage           : %dM\r\n" % (stats.get_value('memusage/max')/1024/1024)
        s += "Current memory usage           : %dM\r\n" % (self.get_virtual_size()/1024/1024)

        s += "ENGINE STATUS ------------------------------------------------------- \r\n"
        s += "\r\n"
        s += pformat(get_engine_status(self.crawler.engine))
        s += "\r\n"
        self.mail.send(rcpts, subject, s)





import os
from six.moves import cPickle as pickle

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.job import job_dir

class SpiderState(object):
    """Store and load spider state during a scraping job"""

    def __init__(self, jobdir=None):
        self.jobdir = jobdir

    @classmethod
    def from_crawler(cls, crawler):
        jobdir = job_dir(crawler.settings)
        if not jobdir:
            raise NotConfigured

        obj = cls(jobdir)
        crawler.signals.connect(obj.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        return obj

    def spider_closed(self, spider):
        if self.jobdir:
            with open(self.statefn, 'wb') as f:
                pickle.dump(spider.state, f, protocol=2)

    def spider_opened(self, spider):
        if self.jobdir and os.path.exists(self.statefn):
            with open(self.statefn, 'rb') as f:
                spider.state = pickle.load(f)
        else:
            spider.state = {}

    @property
    def statefn(self):
        return os.path.join(self.jobdir, 'spider.state')


















class Prometheus(Site):
    """

    """
    def __init__(self, crawler):
        self.crawler = crawler
        self.uuid = uuid.uuid4().hex
        self.name = self.crawler.spider.name
        self.port = crawler.settings.get('PROMETHEUS_PORT', 8888)
        self.host = crawler.settings.get('PROMETHEUS_HOST', '127.0.0.1')
        self.path = crawler.settings.get('PROMETHEUS_PATH', 'metrics')

        self.spider_item_scraped = Counter('spider_items_scraped', 'Number of items scraped', ['name', 'uuid'])
        self.spider_item_dropped = Counter('spider_items_dropped', 'Number of items dropped', ['name', 'uuid'])
        self.spider_response_received = Counter('spider_response_received', 'Number of responses received', ['name', 'uuid'])
        self.spider_opened_count = Counter('spider_opened_count', 'Spider opened count', ['name', 'uuid'])
        self.spider_closed_count = Counter('spider_closed_count', 'Spider closed count', ['name', 'uuid'])

        # from prometheus_client import Gauge
        # g = Gauge('my_inprogress_requests', 'Description of gauge')
        # g.inc()  # Increment by 1
        # g.dec(10)  # Decrement by given value
        # g.set(4.2)  # Set to a given value
        #
        # from prometheus_client import Summary
        # s = Summary('request_latency_seconds', 'Description of summary')
        # s.observe(4.7)  # Observe 4.7 (seconds in this case)

        self.root = Resource()
        self.root.putChild(self.path, MetricsResource())
        self.noisy = False

        crawler.signals.connect(self.start_listening, signals.engine_started)
        crawler.signals.connect(self.stop_listening, signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler):
        o = cls(crawler)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(o.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(o.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(o.response_received, signal=signals.response_received)
        return o

    def start_listening(self):
        print self.crawler.stats.get_stats()
        factory = Site(self.root)
        reactor.listenTCP(self.port, factory)
        reactor.run()

        logger.debug('Exporting metrics on port 8888')

    def stop_listening(self):
        reactor.stop()

    def spider_opened(self, spider):
        self.spider_opened_count.labels(name=self.name, uuid=self.uuid).inc()

    def spider_closed(self, spider, reason):
        self.spider_closed_count.labels(name=self.name, uuid=self.uuid).inc()

    def item_scraped(self, item, spider):
        self.spider_item_scraped.labels(name=self.name, uuid=self.uuid).inc()

    def response_received(self, spider):
        self.spider_response_received.labels(name=self.name, uuid=self.uuid).inc()

    def item_dropped(self, item, spider, exception):
        self.spider_item_scraped.labels(name=self.name, uuid=self.uuid).inc()