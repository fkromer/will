import logging
import inspect
import importlib
import os
import re
import sys
import time
import traceback
from os.path import abspath, join, curdir
from multiprocessing import Process

from gevent import monkey
# Monkeypatch has to come before bottle
monkey.patch_all()
import bottle

from listener import WillXMPPClientMixin
from mixins import ScheduleMixin, StorageMixin, ErrorMixin, HipChatMixin, RoomMixin
from scheduler import Scheduler
import settings


# Force UTF8
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input


# Update path
PROJECT_ROOT = abspath(curdir)
PLUGINS_ROOT = abspath(join(PROJECT_ROOT, "will", "plugins"))
TEMPLATES_ROOT = abspath(join(PROJECT_ROOT, "will", "templates"))
sys.path.append(PROJECT_ROOT)
sys.path.append(join(PROJECT_ROOT, "will"))



class WillBot(WillXMPPClientMixin, StorageMixin, ScheduleMixin, ErrorMixin, RoomMixin, HipChatMixin):

    def __init__(self):
        pass

    def bootstrap(self):
        self.bootstrap_storage()
        self.bootstrap_plugins()

        # Scheduler
        scheduler_thread = Process(target=self.bootstrap_scheduler)
        # scheduler_thread.daemon = True
        
        # Bottle
        bottle_thread = Process(target=self.bootstrap_bottle)
        # bottle_thread.daemon = True

        # XMPP Listener
        xmpp_thread = Process(target=self.bootstrap_xmpp)
        # xmpp_thread.daemon = True

        try:
            # Start up threads.
            xmpp_thread.start()
            scheduler_thread.start()
            bottle_thread.start()

            errors = self.get_startup_errors()
            if len(errors) > 0:
                default_room = self.get_room_from_name_or_id(settings.WILL_DEFAULT_ROOM)["room_id"]
                self.send_room_message(default_room, "FYI, I had some errors starting up:")
                for err in errors:
                    self.send_room_message(default_room, err)

            while True: time.sleep(100)
        except (KeyboardInterrupt, SystemExit):
            scheduler_thread.terminate()
            bottle_thread.terminate()
            xmpp_thread.terminate()
            print '\n\nReceived keyboard interrupt, quitting threads.',
            while scheduler_thread.is_alive() or\
                  bottle_thread.is_alive() or\
                  xmpp_thread.is_alive():
                    sys.stdout.write(".")
                    sys.stdout.flush()
                    time.sleep(0.5)

    def bootstrap_scheduler(self):
        Scheduler.clear_locks(self)
        self.scheduler = Scheduler()
        
        for cls, fn in self.periodic_tasks:
            self.add_periodic_task(cls, fn.sched_args, fn.sched_kwargs, fn,)
        for cls, fn in self.random_tasks:
            self.add_random_tasks(cls, fn, fn.start_hour, fn.end_hour, fn.day_of_week, fn.num_times_per_day)
        self.scheduler.start_loop(self)

    def bootstrap_bottle(self):
        print "Bootstrapping bottle..."

        for cls, function_name in self.bottle_routes:
            instantiated_cls = cls()
            instantiated_fn = getattr(instantiated_cls, function_name)
            bottle.route(instantiated_fn.bottle_route)(instantiated_fn)

        bottle.run(host='localhost', port=settings.WILL_HTTPSERVER_PORT, server='gevent')
        pass

    def bootstrap_xmpp(self):
        print "Bootstrapping xmpp..."
        self.start_xmpp_client()
        self.connect()
        self.process(block=True)

    def bootstrap_plugins(self):
        print "Bootstrapping plugins..."
        plugin_modules = {}

        # Sure does feel like this should be a solved problem somehow.
        for root, dirs, files in os.walk(PLUGINS_ROOT, topdown=False):
            for f in files:
                if f[-3:] == ".py" and f != "__init__.py":
                    module = f[:-3]
                    parent_module = root.replace("%s" % PLUGINS_ROOT, "")
                    if parent_module != "":
                        module_paths = parent_module.split("/")[1:]
                        module_paths.append(module)
                        module_path = ".".join(module_paths)
                    else:
                        module_path = module
                    try:
                        plugin_modules[module] = importlib.import_module("will.plugins.%s" % module_path)
                    except Exception, e:
                        self.startup_error("Error loading will.plugins.%s" % (module_path,), e)

        self.plugins = []
        for name, module in plugin_modules.items():
            try:
                for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                    try:
                        if hasattr(cls, "is_will_plugin") and cls.is_will_plugin and class_name != "WillPlugin":
                            self.plugins.append({"name": class_name, "class": cls})
                    except Exception, e:
                        self.startup_error("Error bootstrapping %s" % (class_name,), e)
            except Exception, e:
                self.startup_error("Error bootstrapping %s" % (name,), e)

        # Sift and Sort.
        self.message_listeners = []
        self.periodic_tasks = []
        self.random_tasks = []
        self.bottle_routes = []
        self.some_listeners_include_me = False
        for plugin_info in self.plugins:
            try:
                print "\n %s:" % plugin_info["name"]
                for function_name, fn in inspect.getmembers(plugin_info["class"], predicate=inspect.ismethod):
                    try:
                        if hasattr(fn, "listens_to_messages") and fn.listens_to_messages and hasattr(fn, "listener_regex"):
                            print " - %s" % function_name
                            regex = fn.listener_regex
                            if not fn.case_sensitive:
                                regex = "(?i)%s" % regex
                            self.message_listeners.append({
                                "function_name": function_name,
                                "regex_pattern": fn.listener_regex,
                                "regex": re.compile(regex),
                                "fn": getattr(plugin_info["class"](), function_name),
                                "args": fn.listener_args,
                                "include_me": fn.listener_includes_me,
                                "direct_mentions_only": fn.listens_only_to_direct_mentions,
                            })
                            if fn.listener_includes_me:
                                self.some_listeners_include_me = True
                        elif hasattr(fn, "periodic_task") and fn.periodic_task:
                            print " - %s" % function_name
                            self.periodic_tasks.append((plugin_info["class"], fn))
                        elif hasattr(fn, "random_task") and fn.random_task:
                            print " - %s" % function_name
                            self.random_tasks.append((plugin_info["class"], fn))
                        elif hasattr(fn, "bottle_route"):
                            print " - %s" % function_name
                            self.bottle_routes.append((plugin_info["class"], function_name))


                    except Exception, e:
                        self.startup_error("Error bootstrapping %s.%s" % (plugin_info["class"], function_name,), e)
            except Exception, e:
                self.startup_error("Error bootstrapping %s" % (plugin_info["class"],), e)
        print "Done.\n"