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
import time
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
    "adb_snap_lines": 5
}
def decode(ind):
    try:
        return ind.decode("utf-8")
    except:
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
    if isinstance(filter, (str, unicode)):
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
    def __init__(self, cmd, name="", device=""):
        self.__name = "ADB: %s" % name
        self.__device = device
        self.__view = None
        self.__last_fold = None
        self.__timer = None
        self.__lines = []
        self.__cond = threading.Condition()
        self.__maxlines = get_setting("adb_maxlines")
        self.__filter = re.compile(get_setting("adb_filter"))
        self.__do_scroll = get_setting("adb_auto_scroll")
        self.__manual_scroll = False
        self.__snapLines = get_setting("adb_snap_lines")
        self.__cmd = cmd
        self.__closing = False
        self.__view = sublime.active_window().new_file()
        self.__view.set_name(self.__name)
        self.__view.set_scratch(True)
        self.__view.set_read_only(True)
        self.__view.set_syntax_file("Packages/ADBView/adb.tmLanguage")

        print("running: %s" % cmd)
        info = None
        if os.name == 'nt':
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.__adb_process = subprocess.Popen(cmd, startupinfo=info, stdout=subprocess.PIPE)
        threading.Thread(target=self.__output_thread, args=(self.__adb_process.stdout,)).start()
        threading.Thread(target=self.__process_thread).start()

    def close(self):
        if self.__adb_process != None and self.__adb_process.poll() == None:
            self.__adb_process.kill()

    def set_filter(self, filter):
        try:
            self.__filter = re.compile(filter)
            if self.__view:
                self.__last_fold = apply_filter(self.__view, self.__filter)
        except:
            traceback.print_exc()
            sublime.error_message("invalid regex")

    @property
    def name(self):
        return self.__name

    @property
    def device(self):
        return self.__device

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
                if not isinstance(line, unicode):
                    line = line.decode("utf-8", "ignore")

                if len(line) > 0:
                    with self.__cond:
                        self.__lines.append(line + "\n")
                        self.__cond.notify()
            except UnicodeDecodeError, e:
                print "[ADBView] UnicodeDecodeError occurred:", e
                print "[ADBView] the line is: ", [ord(c) for c in line]
            except:
                traceback.print_exc()
        def __update_name():
            self.__name += " [Closed]"
            self.__view.set_name(self.__name)
        sublime.set_timeout(__update_name, 0)

        # shutdown the process thread
        with self.__cond:
            self.__closing = True
            self.__cond.notify()

    def __process_thread(self):
        while True:
            with self.__cond:
                if self.__closing:
                    break
                self.__cond.wait()

            # collect more logs, for better performance
            time.sleep(0.01)

            sublime.set_timeout(self.__check_autoscroll, 0)

            lines = None
            with self.__cond:
                lines = self.__lines
                self.__lines = []

            if len(lines) > 0:
                def gen_func(view, lines):
                    def __run():
                        view.run_command("adb_add_line", {"data": lines})
                    return __run
                sublime.set_timeout(gen_func(self.__view, lines), 0)

    def __check_autoscroll(self):
        if self.__do_scroll:
            row, _ = self.__view.rowcol(self.__view.size())
            snap_point = self.__view.text_point(max(0, row - self.__snapLines), 0)
            snap_point = self.__view.text_to_layout(snap_point)[1]
            p = self.__view.viewport_position()[1] + self.__view.viewport_extent()[1]
            ns = p < snap_point
            if ns != self.__manual_scroll:
                self.__manual_scroll = ns
                sublime.status_message("ADB: manual scrolling enabled" if self.__manual_scroll else "ADB: automatic scrolling enabled")

    def process_lines(self, e, lines):
        overflowed = 0
        row, _ = self.__view.rowcol(self.__view.size())
        for line in lines:
            row += 1
            if row > self.__maxlines:
                overflowed += 1
            self.__view.set_read_only(False)
            self.__view.insert(e, self.__view.size(), line)
            self.__view.set_read_only(True)

            if self.__filter.search(line) == None:
                region = self.__view.line(self.__view.size()-1)
                if self.__last_fold != None:
                    self.__last_fold = self.__last_fold.cover(region)
                else:
                    self.__last_fold = region
            else:
                if self.__last_fold is not None:
                    foldregion = sublime.Region(self.__last_fold.begin()-1, self.__last_fold.end())
                    self.__view.fold(foldregion)
                self.__last_fold = None
        if overflowed > 0:
            remove_region = sublime.Region(0, self.__view.text_point(overflowed, 0))
            self.__view.set_read_only(False)
            self.__view.erase(e, remove_region)
            self.__view.set_read_only(True)
            if self.__last_fold is not None:
                self.__last_fold = sublime.Region(self.__last_fold.begin() - remove_region.size(), 
                                                  self.__last_fold.end() - remove_region.size())
        if self.__last_fold is not None:
            foldregion = sublime.Region(self.__last_fold.begin()-1, self.__last_fold.end())
            self.__view.fold(foldregion)
        if self.__do_scroll and not self.__manual_scroll:
            # keep the position of horizontal scroll bar
            curr = self.__view.viewport_position()
            bottom = self.__view.text_to_layout(self.__view.size())
            self.__view.set_viewport_position((curr[0], bottom[1]), True)


################################################################################
#                          Sublime Text 2 Commands                             #
################################################################################

class AdbAddLine(sublime_plugin.TextCommand):
    def run(self, e, data):
        adb_view = get_adb_view(self.view)
        if adb_view:
            adb_view.process_lines(e, data)


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


class AdbFilterByDebuggableApps(sublime_plugin.TextCommand):
    def run(self, edit):
        adb_view = get_adb_view(self.view)
        if adb_view is None:
            return
        device = adb_view.device
        if device == "":
            sublime.error_message("Device is unset")
            return
        adb = get_setting("adb_command")
        cmd = [adb, "-s", device, "jdwp"]
        try:
            proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            out,err = proc.communicate()
            out = decode(out)
        except:
            sublime.error_message("Error trying to launch ADB:\n\n%s\n\n%s" % (cmd, traceback.format_exc()))
            return
        pids = re.findall(r'\d+', out)
        if len(pids) > 0:
            set_filter(self.view, "\( *(%s)\)" % "|".join(pids))
        else:
            sublime.error_message("No debuggable apps")

    def is_enabled(self):
        return is_adb_syntax(self.view)

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByContainingSelections(sublime_plugin.TextCommand):
    def set_filter(self, data):
       set_filter(self.view, data)

    def run(self, edit):
        adb_view = get_adb_view(self.view)
        if adb_view:
            filter = adb_view.filter.pattern
        else:
            filter = get_setting("adb_filter")
        for region in self.view.sel():
            if region.size() == 0:
                continue
            content_re = "(?=.*%s)" % re.escape(self.view.substr(region))
            if filter.startswith("^"):
                filter = "^%s%s" % (content_re, filter[1:])
            else:
                filter = "^%s.*?%s" % (content_re, filter)
        self.set_filter(filter)

    def is_enabled(self):
        return is_adb_syntax(self.view) and any([r.size() > 0 for r in self.view.sel()])

    def is_visible(self):
        return self.is_enabled()


class AdbFilterByExcludingSelections(sublime_plugin.TextCommand):
    def set_filter(self, data):
       set_filter(self.view, data)

    def run(self, edit):
        adb_view = get_adb_view(self.view)
        if adb_view:
            filter = adb_view.filter.pattern
        else:
            filter = get_setting("adb_filter")
        for region in self.view.sel():
            if region.size() == 0:
                continue
            content_re = "(?!.*%s)" % re.escape(self.view.substr(region))
            if filter.startswith("^"):
                filter = "^%s%s" % (content_re, filter[1:])
            else:
                filter = "^%s.*?%s" % (content_re, filter)
        self.set_filter(filter)

    def is_enabled(self):
        return is_adb_syntax(self.view) and any([r.size() > 0 for r in self.view.sel()])

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
            if line.endswith("device"):
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
            self.launch([adb] + args, self.options[0], self.devices[0])
        else:
            self.window.show_quick_panel(self.options, self.on_done)

    def launch(self, cmd, name, device):
        adb_views.append(ADBView(cmd, name, device))

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
        self.launch(cmd, name, device)


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
