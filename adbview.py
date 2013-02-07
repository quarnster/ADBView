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
import os
import sys
try:
    import Queue
except:
    import queue as Queue
import re
import threading
import traceback
import telnetlib


################################################################################
#                             Utility functions                                #
################################################################################
def get_settings():
    return sublime.load_settings("ADBView.sublime-settings")

__adb_settings_defaults = {
    "adb_command": "adb",
    "adb_args": ["logcat", "-v", "time"],
    "adb_maxlines": 20000,
    "adb_filter": ".",
    "adb_auto_scroll": True,
    "adb_launch_single": True,
    "adb_snap_lines": 5,
    "adb_delay_scrolling": True
}
def decode(ind):
    try:
        return ind.decode(sys.getdefaultencoding())
    except:
        return ind

def get_setting(key, view=None, raw=False):
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

            print(msg)

            if show:
                sublime.message_dialog(msg)
        elif key == "adb_args" and value == None:
            cmd = get_setting("adb_command", view, True)
            if type(cmd) == list:
                value = cmd[1:]
        if value == None:
            value = __adb_settings_defaults[key] or None

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


def apply_filter(view, filter):
    if isinstance(filter, str):
        filter = re.compile(filter)
    currRegion = None
    if is_adb_syntax(view):
        view.run_command("unfold_all")
        endline, endcol = view.rowcol(view.size())
        line = 0
        currRegion = None
        regions = []
        while line < endline:
            region = view.full_line(view.text_point(line, 0))
            data = view.substr(region)
            if filter.search(data) == None:
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
    return currRegion


def is_adb_syntax(view):
    if not view:
        return False
    sn = view.scope_name(view.sel()[0].a)
    return sn.startswith("source.adb")

adb_views = []
def get_adb_view(view):
    id = view.id()
    for adb_view in adb_views:
        if adb_view.view.id() == id:
            return adb_view
    return None

def set_filter(view, filter):
    adb_view = get_adb_view(view)
    if adb_view:
        adb_view.set_filter(filter)
    else:
        apply_filter(view, filter)


################################################################################
#                ADBView class dealing with ADB Logcat views                   #
################################################################################
class ADBView(object):
    LINE = 0
    FOLD_ALL = 1
    SCROLL = 3
    VIEWPORT_POSITION = 4

    def __init__(self, cmd, name=""):
        self.__queue = Queue.Queue()
        self.__name = "ADB: %s" % name
        self.__view = None
        self.__last_fold = None
        self.__timer = None
        self.__lines = ""
        self.__lock = threading.RLock()
        self.__maxlines = get_setting("adb_maxlines")
        self.__filter = re.compile(get_setting("adb_filter"))
        self.__doScroll = get_setting("adb_auto_scroll")
        self.__manualScroll = False
        self.__snapLines = get_setting("adb_snap_lines")
        self.__cmd = cmd
        self.__view = sublime.active_window().new_file()
        self.__view.set_name(self.__name)
        self.__view.set_scratch(True)
        self.__view.set_read_only(True)
        self.__view.set_syntax_file("Packages/ADBView/adb.tmLanguage")
        if get_setting("adb_delay_scrolling"):
            self.__loading = threading.Timer(0.25, self.__load_finished)
        else:
            self.__loading = None

        print("running: %s" % cmd)
        info = None
        if os.name == 'nt':
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.__adb_process = subprocess.Popen(cmd, startupinfo=info, stdout=subprocess.PIPE)
        t = threading.Thread(target=self.__output_thread, args=(self.__adb_process.stdout,))
        t.start()

        if self.__loading:
            self.__loading.start()

    def __load_finished(self):
        self.__loading = None
        sublime.set_timeout(lambda: self.__view.show(self.__view.size()), 0)

    def close(self):
        if self.__adb_process != None and self.__adb_process.poll() == None:
            self.__adb_process.kill()

    def __timed_add(self):
        try:
            self.__lock.acquire()
            line = self.__lines
            self.__lines = ""
            self.__timer = None
            self.__queue.put((ADBView.LINE, line))
            sublime.set_timeout(self.__update, 0)
        finally:
            self.__lock.release()

    def add_line(self, line):
        try:
            self.__lock.acquire()
            self.__lines += line

            if self.__loading:
                self.__loading.cancel()
                self.__loading = threading.Timer(0.25, self.__load_finished)
                self.__loading.start()

            if self.__timer:
                self.__timer.cancel()
            if self.__lines.count("\n") > 10:
                self.__timed_add()
            else:
                self.__timer = threading.Timer(0.1, self.__timed_add)
                self.__timer.start()
        finally:
            self.__lock.release()

    def scroll(self, line):
        self.__queue.put((ADBView.SCROLL, line))
        sublime.set_timeout(self.__update, 0)

    def set_viewport_position(self, pos):
        self.__queue.put((ADBView.VIEWPORT_POSITION, pos))
        sublime.set_timeout(self.__update, 0)

    def set_filter(self, filter):
        try:
            self.__filter = re.compile(filter)
            if self.__view:
                self.__last_fold = apply_filter(self.__view, self.__filter)
        except:
            traceback.print_exc()
            sublime.error_message("invalid regex")

    def fold_all(self):
        self.__queue.put((ADBView.FOLD_ALL, None))

    @property
    def name(self):
        return self.__name

    @property
    def view(self):
        return self.__view

    @property
    def filter(self):
        return self.__filter

    @property
    def running(self):
        return self.__adb_process.poll() == None

    def __output_thread(self, pipe):
        while True:
            try:
                if self.__adb_process.poll() != None:
                    break
                line = decode(pipe.readline().strip())

                if len(line) > 0:
                    self.add_line("%s\n" % line)
            except:
                traceback.print_exc()
        def __update_name():
            self.__name += " [Closed]"
            self.__view.set_name(self.__name)
        sublime.set_timeout(__update_name, 0)

    def process_lines(self, e, data):
        for line in data.split("\n"):
            if len(line.strip()) == 0:
                continue
            line += "\n"
            row, col = self.__view.rowcol(self.__view.size())
            self.__view.set_read_only(False)

            if row+1 > self.__maxlines:
                self.__view.erase(e, self.__view.full_line(0))
            self.__view.insert(e, self.__view.size(), line)
            self.__view.set_read_only(True)

            if self.__filter.search(line) == None:
                region = self.__view.line(self.__view.size()-1)
                if self.__last_fold != None:
                    self.__view.unfold(self.__last_fold)
                    self.__last_fold = self.__last_fold.cover(region)
                else:
                    self.__last_fold = region
                foldregion = sublime.Region(self.__last_fold.begin()-1, self.__last_fold.end())
                self.__view.fold(foldregion)
            else:
                self.__last_fold = None
        if not self.__loading and self.__doScroll and not self.__manualScroll:
            self.__view.show(self.__view.size())

    def __update(self):
        try:
            while True:
                cmd, data = self.__queue.get_nowait()
                if cmd == ADBView.LINE:
                    if not self.__loading and self.__doScroll:
                        snapPoint = self.__view.size()
                        for i in range(self.__snapLines):
                            snapPoint = self.__view.line(snapPoint).begin()-1
                        snapPoint = self.__view.text_to_layout(snapPoint)[1]
                        p = self.__view.viewport_position()[1] + self.__view.viewport_extent()[1]
                        ns = p < snapPoint
                        if ns != self.__manualScroll:
                            self.__manualScroll = ns
                            sublime.status_message("ADB: manual scrolling enabled" if self.__manualScroll else "ADB: automatic scrolling enabled")
                    self.__view.run_command("adb_add_line", {"data": data})
                elif cmd == ADBView.FOLD_ALL:
                    self.__view.run_command("fold_all")
                elif cmd == ADBView.SCROLL:
                    self.__view.run_command("goto_line", {"line": data + 1})
                elif cmd == ADBView.VIEWPORT_POSITION:
                    self.__view.set_viewport_position(data, True)
                self.__queue.task_done()
        except Queue.Empty:
            # get_nowait throws an exception when there's nothing..
            pass
        except:
            traceback.print_exc()


################################################################################
#                          Sublime Text 2 Commands                             #
################################################################################

class AdbAddLine(sublime_plugin.TextCommand):
    def run(self, e, data):
        get_adb_view(self.view).process_lines(e, data)

class AdbFilterByProcessId(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*./.+\( *(\d+)\)", data)
        if match != None:
            set_filter(self.view, "\( *%s\)" % match.group(1))
        else:
            sublime.error_message("Couldn't extract process id")

    def is_enabled(self):
        return is_adb_syntax(self.view)

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByProcessName(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*./(.+)\( *\d+\)", data)
        if match != None:
            set_filter(self.view, "%s\( *\d+\)" % match.group(1))
        else:
            sublime.error_message("Couldn't extract process name")

    def is_enabled(self):
        return is_adb_syntax(self.view)

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByMessageLevel(sublime_plugin.TextCommand):
    def run(self, edit):
        data = self.view.substr(self.view.full_line(self.view.sel()[0].a))
        match = re.match(r"[\-\d\s:.]*(\w)/.+\( *\d+\)", data)
        if match != None:
            set_filter(self.view, "%s/.+\( *\d+\)" % match.group(1))
        else:
            sublime.error_message("Couldn't extract Message level")

    def is_enabled(self):
        return is_adb_syntax(self.view)

    def is_visible(self):
        return self.is_enabled()


class AdbLaunch(sublime_plugin.WindowCommand):
    def run(self):
        adb = get_setting("adb_command")
        cmd = [adb, "devices"]
        try:
            proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            out,err = proc.communicate()
            out = decode(out)
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
        self.options = []
        for view in adb_views:
            self.options.append([view.name, "Focus existing view"])
        for device in self.devices:
            # dump build.prop
            cmd = [adb, "-s", device, "shell", "cat /system/build.prop"]
            proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            build_prop = decode(proc.stdout.read().strip())
            # get name
            product = "Unknown"  # should never actually see this
            if device.startswith("emulator"):
                port = int(device.rsplit("-")[-1])
                t = telnetlib.Telnet("localhost", port)
                t.read_until(b"OK", 1000)
                t.write(b"avd name\n")
                product = t.read_until(b"OK", 1000).decode("utf-8")
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
            product = str(product).strip()
            version = str(version).strip()
            device = str(device).strip()
            self.options.append("%s %s - %s" % (product, version, device))

        if len(self.options) == 0:
            sublime.status_message("ADB: No device attached!")
        elif len(self.options) == 1 and len(adb_views) == 0 and get_setting("adb_launch_single"):
            adb = get_setting("adb_command")
            args = get_setting("adb_args")
            self.launch([adb] + args, self.options[0])
        else:
            self.window.show_quick_panel(self.options, self.on_done)

    def launch(self, cmd, name):
        adb_views.append(ADBView(cmd, name))

    def on_done(self, picked):
        if picked == -1:
            return
        if picked < len(adb_views):
            view = adb_views[picked].view
            window = view.window()
            if window == None:
                # This is silly, but apparently the view is considered windowless
                # when it is not focused
                found = False
                for window in sublime.windows():
                    for view2 in window.views():
                        if view2.id() == view.id():
                            found = True
                            break
                    if found:
                        break
            window.focus_view(view)
            return
        name = self.options[picked]
        picked -= len(adb_views)
        device = self.devices[picked]
        adb = get_setting("adb_command")
        args = get_setting("adb_args")
        cmd = [adb, "-s", device] + args
        self.launch(cmd, name)

class AdbSetFilter(sublime_plugin.TextCommand):
    def set_filter(self, data):
       set_filter(self.view, data)

    def run(self, edit):
        adb_view = get_adb_view(self.view)
        if adb_view:
            filter = adb_view.filter.pattern
        else:
            filter = get_setting("adb_filter")
        self.view.window().show_input_panel("ADB Regex filter", filter, self.set_filter, None, None)

    def is_enabled(self):
        return is_adb_syntax(self.view)

    def is_visible(self):
        return self.is_enabled()


class AdbClearView(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.set_read_only(False)
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.set_read_only(True)

    def is_enabled(self):
        adb_view = get_adb_view(self.view)
        return adb_view != None

    def is_visible(self):
        return self.is_enabled()


class AdbEventListener(sublime_plugin.EventListener):
    def on_close(self, view):
        adb_view = get_adb_view(view)
        if adb_view:
            adb_view.close()
            adb_views.remove(adb_view)
        view.settings().erase("adb_has_shown_message")
