import sublime
import sublime_plugin
import re
import string
from itertools import izip_longest
from pprint import pprint

REGEX_ESCAPE_CHARS = '\\+*()[]{}^$?|:].,'

# not a fan of using globals like this, but not sure if there's a better way with the plugin
# API that ST2 provides.  Tried attaching as fields to active_view, but didn't persiste, I'm guessing
# it's just a representation of something that gets regenerated on demand so dynamic fields are transient
JUMP_GROUP_ITERATOR = None
CURRENT_JUMP_GROUP = None
EASY_MOTION_EDIT = None
SELECT_TEXT = False
COMMAND_MODE_WAS = False


class JumpGroupIterator:
    '''
       given a list of region jump targets matching the given character, can emit a series of
       JumpGroup dictionaries
    '''
    def __init__(self, view, character, placeholder_chars):
        self.view = view
        self.all_jump_targets = self.find_all_jump_targets_in_visible_region(character)
        self.interleaved_jump_targets = self.interleave_jump_targets_from_cursor()
        self.jump_target_index = 0
        self.placeholder_chars = placeholder_chars

    def __iter__(self):
        return self

    def interleave_jump_targets_from_cursor(self):
        sel = self.view.sel()[0]  # multi select not supported, doesn't really make sense
        sel_begin = sel.begin()
        sel_end = sel.end()
        before = []
        after = []

        # split them into two lists radiating out from the cursor position
        for target in self.all_jump_targets:
            if target.begin() < sel_begin:
                # add to beginning of list so closest targets to cursor are first
                before.insert(0, target)
            elif target.begin() > sel_end:
                after.append(target)

        # now interleave the two lists together into one list
        return [target for targets in izip_longest(before, after) for target in targets if target is not None]

    def has_next(self):
        return self.jump_target_index < len(self.interleaved_jump_targets)

    def next(self):
        if not self.has_next():
            raise StopIteration

        jump_group = dict()

        for placeholder_char in self.placeholder_chars:
            if self.has_next():
                jump_group[placeholder_char] = self.interleaved_jump_targets[self.jump_target_index]
                self.jump_target_index += 1
            else:
                break

        return jump_group

    def reset(self):
        self.jump_target_index = 0

    def find_all_jump_targets_in_visible_region(self, character):
        visible_region_begin = self.visible_region_begin()
        visible_text = self.visible_text()
        folded_regions = self.get_folded_regions(self.view)
        matching_regions = []
        escaped_character = self.escape_character(character)

        for char_at in (match.start() for match in re.finditer(escaped_character, visible_text)):
            char_point = char_at + visible_region_begin
            char_region = sublime.Region(char_point, char_point + 1)
            if not self.region_list_contains_region(folded_regions, char_region):
                matching_regions.append(char_region)

        return matching_regions

    def region_list_contains_region(self, region_list, region):

        for element_region in region_list:
            if element_region.contains(region):
                return True
        return False

    def visible_region_begin(self):
        return self.view.visible_region().begin()

    def visible_text(self):
        visible_region = self.view.visible_region()
        return self.view.substr(visible_region)

    def escape_character(self, character):
        if (REGEX_ESCAPE_CHARS.find(character) >= 0):
            return '\\' + character
        else:
            return character

    def get_folded_regions(self, view):
        '''
        No way in the API to get the folded regions without unfolding them first
        seems to be quick enough that you can't actually see them fold/unfold
        '''
        folded_regions = view.unfold(view.visible_region())
        view.fold(folded_regions)
        return folded_regions


class EasyMotionCommand(sublime_plugin.WindowCommand):
    winning_selection = None
    def run(self, character=None, select_text=False):
        global JUMP_GROUP_ITERATOR, SELECT_TEXT, COMMAND_MODE_WAS
        sublime.status_message("EasyMotion: Jump to " + character)

        active_view = self.window.active_view()
        active_view.settings().set('easy_motion_mode', True)
        # yes, this feels a little dirty to mess with the Vintage plugin, but there
        # doesn't appear to be any other way to tell it to not intercept keys, so turn it
        # off (if it's on) while we're running EasyMotion
        COMMAND_MODE_WAS = active_view.settings().get('command_mode')
        if (COMMAND_MODE_WAS):
            active_view.settings().set('command_mode', False)

        settings = sublime.load_settings("EasyMotion.sublime-settings")
        placeholder_chars = settings.get('placeholder_chars', 'abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ')

        JUMP_GROUP_ITERATOR = JumpGroupIterator(active_view, character, placeholder_chars)

        if JUMP_GROUP_ITERATOR.has_next():
            self.window.run_command("show_jump_group")
        else:
            sublime.status_message("EasyMotion: unable to find any instances of " + character + " in visible region")


# TODO make escape/ctrl-c cancel out of EasyMotion
# TODO set up timer to reverse things if user doesn't act?
class ShowJumpGroup(sublime_plugin.WindowCommand):
    jump_target_scope = None
    active_view = None

    def run(self):
        pprint("ShowJumpGroup called")
        # TODO move this call to parse the preferences somewhere else, possibly into the view settings?
        settings = sublime.load_settings("Preferences.sublime-settings")
        self.jump_target_scope = settings.get('jump_target_scope', 'string')

        self.active_view = self.window.active_view()

        self.show_next_jump_group()

    def show_next_jump_group(self):
        global JUMP_GROUP_ITERATOR, CURRENT_JUMP_GROUP
        if not JUMP_GROUP_ITERATOR.has_next():
            JUMP_GROUP_ITERATOR.reset()

        CURRENT_JUMP_GROUP = JUMP_GROUP_ITERATOR.next()
        self.activate_current_jump_group()

    def activate_current_jump_group(self):
        global CURRENT_JUMP_GROUP, EASY_MOTION_EDIT
        '''
            Start up an edit object if we don't have one already, then create all of the jump targets
        '''
        if (EASY_MOTION_EDIT is not None):
            # normally would call deactivate_current_jump_group here, but apparent ST2 bug prevents it from calling undo correctly
            # instead just decorate the new character and keep the same edit object so all changes get undone properly
            self.active_view.erase_regions("jump_match_regions")
        else:
            EASY_MOTION_EDIT = self.active_view.begin_edit()

        for placeholder_char in CURRENT_JUMP_GROUP.keys():
            self.active_view.replace(EASY_MOTION_EDIT, CURRENT_JUMP_GROUP[placeholder_char], placeholder_char)

        self.active_view.add_regions("jump_match_regions", CURRENT_JUMP_GROUP.values(), self.jump_target_scope, "dot")


class JumpTo(sublime_plugin.WindowCommand):
    def run(self, character=None):
        global COMMAND_MODE_WAS

        self.winning_selection = self.winning_selection_from(character)
        self.active_view = self.window.active_view()
        self.finish_easy_motion()
        self.active_view.settings().set('easy_motion_mode', False)
        if (COMMAND_MODE_WAS):
            self.active_view.settings().set('command_mode', True)

    def winning_selection_from(self, selection):
        global CURRENT_JUMP_GROUP, SELECT_TEXT
        winning_region = None
        if selection in CURRENT_JUMP_GROUP:
            winning_region = CURRENT_JUMP_GROUP[selection]

        if winning_region is not None:
            if SELECT_TEXT:
                for current_selection in self.active_view.sel():
                    if winning_region.begin() < current_selection.begin():
                        return sublime.Region(current_selection.end(), winning_region.begin())
                    else:
                        return sublime.Region(current_selection.begin(), winning_region.end())
            else:
                return sublime.Region(winning_region.begin(), winning_region.begin())

    def finish_easy_motion(self):
        '''
        We need to clean up after ourselves by restoring the view to it's original state, if the user did
        press a jump target that we've got saved, jump to it as the last action
        '''
        self.deactivate_current_jump_group()
        self.jump_to_winning_selection()

    def deactivate_current_jump_group(self):
        '''
            Close out the edit that we've been messing with and then undo it right away to return the buffer to
            the pristine state that we found it in.  Other methods ended up leaving the window in a dirty save state
            and this seems to be the cleanest way to get back to the original state
        '''
        global EASY_MOTION_EDIT
        if (EASY_MOTION_EDIT is not None):
            self.active_view.end_edit(EASY_MOTION_EDIT)
            self.window.run_command("undo")
            EASY_MOTION_EDIT = None

        self.active_view.erase_regions("jump_match_regions")

    def jump_to_winning_selection(self):
        if self.winning_selection is not None:
            self.active_view.run_command("jump_to_winning_selection", {"begin": self.winning_selection.begin(), "end": self.winning_selection.end()})


class JumpToWinningSelection(sublime_plugin.TextCommand):
    def run(self, edit, begin, end):
        winning_region = sublime.Region(long(begin), long(end))
        sel = self.view.sel()
        sel.clear()
        sel.add(winning_region)
        self.view.show(winning_region)
