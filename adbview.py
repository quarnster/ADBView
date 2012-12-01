"""
Copyright (c) 2012 Fredrik Ehnbom

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
import sublime
import sublime_plugin
import subprocess
import Queue
import re
import threading
import traceback
import telnetlib


def get_settings():
    return sublime.load_settings("ADBView.sublime-settings")


def get_setting(key, default=None, view=None, raw=False):
    def myret(key, value):
        if raw:
            return value
        if key == "adb_command" and type(value) == list:
            args = value[1:]
            value = value[0]
            msg = """The adb_command setting was changed from a list to a string, with the arguments in the separate setting \"adb_args\". \
 The setting for this view has been automatically converted, but you'll need to change the source of this setting for it to persist. The automatic conversion is now using these settings:

 "adb_command": "%s",
 "adb_args": %s,

 (Hint, this message is also printed in the python console for easy copy'n'paste)""" % (value, args)
            show = True
            try:
                show = not view.settings().get("adb_has_shown_message", False)
                view.settings().set("adb_has_shown_message", True)
            except:
                pass

            print msg

            if show:
                sublime.message_dialog(msg)
        elif key == "adb_args" and value == None:
            cmd = get_setting("adb_command", view, True)
            if type(cmd) == list:
                value = cmd[1:]
            else:
                value = default
        return value

    try:
        if view == None:
            view = sublime.active_window().active_view()
        s = view.settings()
        if s.has(key):
            return myret(key, s.get(key))
    except:
        traceback.print_exc()
        pass
    return myret(key, get_settings().get(key))


class ADBView(object):
    LINE = 0
    FOLD_ALL = 1
    CLEAR = 2
    SCROLL = 3
    VIEWPORT_POSITION = 4

    def __init__(self):
        self.queue = Queue.Queue()
        self.name = "ADB"
        self.closed = True
        self.view = None
        self.last_fold = None
        self.timer = None
        self.lines = ""
        self.lock = threading.RLock()
        self.maxlines = get_setting("adb_maxlines", 20000)
        self.filter = re.compile(get_setting("adb_filter", "."))
        self.doScroll = get_setting("adb_auto_scroll", True)

    def is_open(self):
        return not self.closed

    def open(self):
        if self.view == None or self.view.window() == None:
            self.create_view()

    def timed_add(self):
        try:
            self.lock.acquire()
            line = self.lines
            self.lines = ""
            self.timer = None
            self.queue.put((ADBView.LINE, line))
            sublime.set_timeout(self.update, 0)
        finally:
            self.lock.release()

    def add_line(self, line):
        if self.is_open():
            try:
                self.lock.acquire()
                self.lines += line
                if self.timer:
                    self.timer.cancel()
                if self.lines.count("\n") > 10:
                    self.timed_add()
                else:
                    self.timer = threading.Timer(0.1, self.timed_add)
                    self.timer.start()
            finally:
                self.lock.release()

    def scroll(self, line):
        if self.is_open():
            self.queue.put((ADBView.SCROLL, line))
            sublime.set_timeout(self.update, 0)

    def set_viewport_position(self, pos):
        if self.is_open():
            self.queue.put((ADBView.VIEWPORT_POSITION, pos))
            sublime.set_timeout(self.update, 0)

    def clear(self):
        if self.is_open():
            self.queue.put((ADBView.CLEAR, None))
            sublime.set_timeout(self.update, 0)

    def set_filter(self, filter, extra_view=None):
        try:
            self.filter = re.compile(filter)
            if extra_view:
                self.apply_filter(extra_view)
            if self.view:
                self.apply_filter(self.view)
        except:
            traceback.print_exc()
            sublime.error_message("invalid regex")

    def apply_filter(self, view):
        if is_adb_syntax(view):
            view.run_command("unfold_all")
            endline, endcol = view.rowcol(view.size())
            line = 0
            currRegion = None
            regions = []
            while line < endline:
                region = view.full_line(view.text_point(line, 0))
                data = view.substr(region)
                if self.filter.search(data) == None:
                    if currRegion == None:
                        currRegion = region
                    else:
                        currRegion = currRegion.cover(region)
                else:
                    if currRegion:
                        # The -1 is to not include the \n and thus making the fold ... appear
                        # at the end of the last line in the fold, rather than at the
                        # beginning of the "accepted" line
                        currRegion = sublime.Region(currRegion.begin()-1, currRegion.end()-1)
                        regions.append(currRegion)
                        currRegion = None
                line += 1
            if currRegion:
                regions.append(currRegion)
            view.fold(regions)
            if self.view and view.id() == self.view.id():
                self.last_fold = currRegion

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        self.view.set_syntax_file("Packages/ADBView/adb.tmLanguage")
        self.closed = False

    def is_closed(self):
        return self.closed

    def was_closed(self):
        self.closed = True

    def fold_all(self):
        if self.is_open():
            self.queue.put((ADBView.FOLD_ALL, None))

    def get_view(self):
        return self.view

    def update(self):
        if not self.is_open():
            return
        try:
            while True:
                cmd, data = self.queue.get_nowait()
                if cmd == ADBView.LINE:
                    for line in data.split("\n"):
                        if len(line.strip()) == 0:
                            continue
                        line += "\n"
                        row, col = self.view.rowcol(self.view.size())
                        e = self.view.begin_edit()
                        self.view.set_read_only(False)

                        if row+1 > self.maxlines:
                            self.view.erase(e, self.view.full_line(0))
                        self.view.insert(e, self.view.size(), line)
                        self.view.end_edit(e)
                        self.view.set_read_only(True)

                        if self.filter.search(line) == None:
                            region = self.view.line(self.view.size()-1)
                            if self.last_fold != None:
                                self.view.unfold(self.last_fold)
                                self.last_fold = self.last_fold.cover(region)
                            else:
                                self.last_fold = region
                            foldregion = sublime.Region(self.last_fold.begin()-1, self.last_fold.end())
                            self.view.fold(foldregion)
                        else:
                            self.last_fold = None
                elif cmd == ADBView.FOLD_ALL:
                    self.view.run_command("fold_all")
                elif cmd == ADBView.CLEAR:
                    self.view.set_read_only(False)
                    e = self.view.begin_edit()
                    self.view.erase(e, sublime.Region(0, self.view.size()))
                    self.view.end_edit(e)
                    self.view.set_read_only(True)
                elif cmd == ADBView.SCROLL:
                    self.view.run_command("goto_line", {"line": data + 1})
                elif cmd == ADBView.VIEWPORT_POSITION:
                    self.view.set_viewport_position(data, True)
                self.queue.task_done()
        except Queue.Empty:
            # get_nowait throws an exception when there's nothing..
            pass
        except:
            traceback.print_exc()
        finally:
            if self.doScroll:
                self.view.show(self.view.size())


adb_view = ADBView()
adb_process = None


def output(pipe):
    while True:
        try:
            if adb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                adb_view.add_line("%s\n" % line)
        except:
            traceback.print_exc()


def is_adb_syntax(view):
    if not view:
        return False
    sn = view.scope_name(view.sel()[0].a)
    return sn.startswith("source.adb")


class AdbFilterByProcessId(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*./.+\( *(\d+)\)", data)
        if match != None:
            adb_view.set_filter("\( *%s\)" % match.group(1), self.view)
        else:
            sublime.error_message("Couldn't extract process id")

    def is_enabled(self):
        return is_adb_syntax(self.view) or (adb_view.is_open() and adb_view.get_view().id() == self.view.id())

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByProcessName(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*./(.+)\( *\d+\)", data)
        if match != None:
            adb_view.set_filter("%s\( *\d+\)" % match.group(1), self.view)
        else:
            sublime.error_message("Couldn't extract process name")

    def is_enabled(self):
        return is_adb_syntax(self.view) or (adb_view.is_open() and adb_view.get_view().id() == self.view.id())

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByMessageLevel(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*(\w)/.+\( *\d+\)", data)
        if match != None:
            adb_view.set_filter("%s/.+\( *\d+\)" % match.group(1), self.view)
        else:
            sublime.error_message("Couldn't extract Message level")

    def is_enabled(self):
        return is_adb_syntax(self.view) or (adb_view.is_open() and adb_view.get_view().id() == self.view.id())

    def is_visible(self):
        return self.is_enabled()


class AdbLaunch(sublime_plugin.WindowCommand):
    def run(self):
        adb = get_setting("adb_command", "adb")
        cmd = [adb, "devices"]
        try:
            proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            out,err = proc.communicate()
        except:
            sublime.error_message("Error trying to launch ADB:\n\n%s\n\n%s" % (cmd, traceback.format_exc()))
            return
        # get list of device ids
        self.devices = []
        for line in out.split("\n"):
            line = line.strip()
            if line not in ["", "List of devices attached"]:
                self.devices.append(re.sub(r"[ \t]*device$", "", line))
        # build quick menu options displaying name, version, and device id
        options = []
        for device in self.devices:
            # dump build.prop
            cmd = [adb, "-s", device, "shell", "cat /system/build.prop"]
            proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            build_prop = proc.stdout.read().strip()
            # get name
            product = "Unknown"  # should never actually see this
            if device.startswith("emulator"):
                port = device.rsplit("-")[-1]
                t = telnetlib.Telnet("localhost", port)
                t.read_until("OK", 1000)
                t.write("avd name\n")
                product = t.read_until("OK", 1000)
                t.close()
                product = product.replace("OK", "").strip()
            else:
                product = re.findall(r"^ro\.product\.model=(.*)$", build_prop, re.MULTILINE)
                if product:
                    product = product[0]
            # get version
            version = re.findall(r"ro\.build\.version\.release=(.*)$", build_prop, re.MULTILINE)
            if version:
                version = version[0]
            else:
                version = "x.x.x"
            options.append("%s %s - %s" % (product, version, device))
        if len(options) == 0:
            sublime.status_message("ADB: No device attached!")
        elif len(options) == 1:
            adb = get_setting("adb_command", "adb")
            args = get_setting("adb_args", ["logcat"])
            self.launch([adb] + args)
        else:
            self.window.show_quick_panel(options, self.on_done)

    def launch(self, cmd):
        global adb_process
        if adb_process != None and adb_process.poll() == None:
            adb_process.kill()
        print "running: %s" % cmd
        adb_process = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
        adb_view.open()
        t = threading.Thread(target=output, args=(adb_process.stdout,))
        t.start()

    def on_done(self, picked):
        if picked == -1:
            return
        device = self.devices[picked]
        adb = get_setting("adb_command", "adb")
        args = get_setting("adb_args", ["logcat"])
        cmd = [adb, "-s", device] + args
        self.launch(cmd)

    def is_enabled(self):
        return not (adb_view.is_open() and adb_view.view.window() != None)


class AdbSetFilter(sublime_plugin.WindowCommand):
    def set_filter(self, data):
        extra_view = None
        w = sublime.active_window()
        if w:
            extra_view = w.active_view()
        adb_view.set_filter(data, extra_view)

    def run(self):
        self.window.show_input_panel("ADB Regex filter", adb_view.filter.pattern, self.set_filter, None, None)

    def is_enabled(self):
        return is_adb_syntax(sublime.active_window().active_view()) or (adb_process != None and adb_view.is_open())

    def is_visible(self):
        return self.is_enabled()


class AdbClearView(sublime_plugin.WindowCommand):
    def run(self):
        adb_view.clear()

    def is_enabled(self):
        return adb_process != None and adb_view.is_open()

    def is_visible(self):
        return self.is_enabled()


class AdbEventListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if adb_view.is_open() and view.id() == adb_view.get_view().id():
            adb_view.was_closed()
            adb_process.kill()
