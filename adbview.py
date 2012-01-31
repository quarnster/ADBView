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
import threading
import traceback


class ADBView(object):
    LINE = 0
    FOLD_ALL = 1
    CLEAR = 2
    SCROLL = 3
    VIEWPORT_POSITION = 4

    def __init__(self, s=True, settingsprefix=None):
        self.queue = Queue.Queue()
        self.name = "ADB"
        self.closed = True
        self.doScroll = s
        self.view = None
        self.settingsprefix = settingsprefix

    def is_open(self):
        return not self.closed

    def open(self):
        if self.view == None or self.view.window() == None:
            self.create_view()

    def add_line(self, line):
        if self.is_open():
            self.queue.put((ADBView.LINE, line))
            sublime.set_timeout(self.update, 0)

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
        insert = ""
        try:
            while True:
                cmd, data = self.queue.get_nowait()
                if cmd == ADBView.LINE:
                    insert += data
                elif cmd == ADBView.FOLD_ALL:
                    self.view.run_command("fold_all")
                elif cmd == ADBView.CLEAR:
                    insert = ""
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
            if len(insert) > 0:
                self.view.set_read_only(False)
                e = self.view.begin_edit()
                self.view.insert(e, self.view.size(), insert)
                self.view.end_edit(e)
                self.view.set_read_only(True)
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


class AdbLaunch(sublime_plugin.WindowCommand):
    def run(self):
        global adb_process
        if adb_process == None or adb_process.poll() != None:
            cmd = ["adb", "logcat"]
            v = self.window.active_view()
            if not v is None:
                cmd = v.settings().get("adb_command", ["adb", "logcat"])
            adb_process = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
            adb_view.open()
            t = threading.Thread(target=output, args=(adb_process.stdout,))
            t.start()

    def is_enabled(self):
        return True


class AdbEventListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if adb_view.is_open() and view.id() == adb_view.get_view().id():
            adb_view.was_closed()
            adb_process.kill()
