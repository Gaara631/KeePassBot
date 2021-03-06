import base64
import datetime
import logging
import math
import uuid as uuid_generator
from abc import ABC
from enum import Enum
from io import BytesIO

import libkeepass
from lxml import etree
from lxml import objectify
from lxml.etree import Element, SubElement
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.lib.db import save_object
from src.models import User, DBSession
from src.settings import NUMBER_OF_ENTRIES_ON_PAGE, lock_emo, arrow_up_emo, arrow_left_emo, arrow_right_emo, black_x_emo, x_emo, repeat_emo, pencil_emo, arrow_down_emo, back_emo, new_line, key_emo, folder_emo

logger = logging.getLogger(__name__)

class ItemType(Enum):
    GROUP = "Group"
    ENTRY = "Entry"
    STRING = "String"
    MANAGER = "Manager"
    AUTOTYPE = "AutoType"

    def __str__(self):
        return self.value


class ProcessType(Enum):
    ADD = "Add"
    EDIT = "Edit"


class BaseKeePass(ABC):

    def __init__(self, root, parent):
        self._root = root
        self._parent = parent
        self.page = 1
        self.size = 0
        self.type = None

    def next_page(self):
        if self.page < self.size / NUMBER_OF_ENTRIES_ON_PAGE:
            self.page += 1
        else:
            self.page = 1

    def previous_page(self):
        if self.page > 1:
            self.page -= 1
        else:
            self.page = int(math.ceil(self.size / NUMBER_OF_ENTRIES_ON_PAGE))

    def activate(self):
        self._root.active_item = self

    def deactivate(self):
        if self._root.active_item == self._root.search_group:
            self._root.search_group = None
        self._root.active_item = self._parent

    def get_root(self):
        return self._root

    def get_parent(self):
        return self._parent

    def delete(self):
        raise NotImplemented("Implement this method in child")


class KeePass:
    def __init__(self, path):
        self.active_item = None
        self.name = "KeePassManager"
        self.type = ItemType.MANAGER
        self.path = path
        self.opened = False
        self.add_edit_state = None
        self._root_obj = None
        self.root_group = None
        self.search_group = None
        self.user = None
        self.kdb = None

    def __str__(self):
        if not self.opened:
            raise IOError("Databse not opened")
        return str(self.root_group)

    def generate_root(self):
        base = Element("KeePassFile")

        meta = self._root_obj.find("Meta")
        base.append(meta)

        root = Element("Root")
        root.append(self.root_group.get_xml_element())
        SubElement(root, "DeletedObjects")

        base.append(root)

        # print(etree.tounicode(base_el, pretty_print=True))

        self._root_obj = objectify.fromstring(etree.tounicode(base), objectify.makeparser())

    def get_user(self):
        return self.user

    def _init_root_group(self):
        if not self.opened:
            raise IOError("Databse not opened")

        def inner_init(parent_group_obj, cur_group):
            for item in parent_group_obj.findall('Group') + parent_group_obj.findall('Entry'):
                if item.tag == str(ItemType.GROUP):
                    new_group = KeeGroup(root=self, parent=cur_group, name=item.Name.text, notes=item.Notes.text, icond_id=item.IconID.text, uuid=item.UUID.text)
                    cur_group.append(new_group)
                    inner_init(item, new_group)
                if item.tag == str(ItemType.ENTRY):
                    asoc = None
                    if hasattr(item.AutoType, 'Association'):
                        asoc = {'window': item.AutoType.Association.Window.text,
                                'key_sec': item.AutoType.Association.KeystrokeSequence.text}
                    autotype = AutoType(enabled=item.AutoType.Enabled.text, dto=item.AutoType.DataTransferObfuscation.text, association=asoc)
                    new_entry = KeeEntry(root=self, parent=cur_group, icond_id=item.IconID.text, uuid=item.UUID.text, autotype=autotype)
                    for string in item.findall('String'):
                        new_entry.append(EntryString(root=self, parent=new_entry, key=string.Key.text, value=string.Value.text))
                    cur_group.append(new_entry)

        root_group_obj = self._root_obj.find('./Root/Group')
        self.root_group = KeeGroup(root=self, parent=None, name=root_group_obj.Name.text, notes=root_group_obj.Notes.text, icond_id=root_group_obj.IconID.text, uuid=root_group_obj.UUID.text)
        inner_init(root_group_obj, self.root_group)
        pass

    def _generate_keyboard(self):
        if not self.opened:
            raise IOError("Databse not opened")

        message_buttons = [[InlineKeyboardButton(text=arrow_left_emo, callback_data="Left"),
                            InlineKeyboardButton(text=arrow_up_emo, callback_data="Back"),
                            InlineKeyboardButton(text=lock_emo, callback_data="Lock"),
                            InlineKeyboardButton(text=arrow_right_emo, callback_data="Right")]]
        if self.active_item == self.root_group:
            delete_but = InlineKeyboardButton(text=black_x_emo, callback_data="Nothing")
        else:
            delete_but = InlineKeyboardButton(text=x_emo, callback_data="Delete")
        second_row = \
            [InlineKeyboardButton(text=pencil_emo, callback_data=f"Edit_{self.active_item.uuid}"),
             InlineKeyboardButton(text=repeat_emo, callback_data="Resend"),
             InlineKeyboardButton(text=arrow_down_emo, callback_data="Download"),
             delete_but]

        if self.active_item and self.active_item.type == ItemType.ENTRY:
            if getattr(self.active_item, 'really_delete', False):
                message_buttons.append([InlineKeyboardButton(text="Yes, I am sure" + x_emo, callback_data="ReallyDelete"),
                                        InlineKeyboardButton(text="No, keep it" + back_emo, callback_data="NoDelete")])
            elif not self.search_group:
                message_buttons.append(second_row)
            InlineKeyboardMarkup(message_buttons)
        else:
            i = 0
            if self.active_item != self.root_group:
                if getattr(self.active_item, 'really_delete', False):
                    message_buttons.append([InlineKeyboardButton(text="Yes, I am sure" + x_emo, callback_data="ReallyDelete"),
                                            InlineKeyboardButton(text="No, keep it" + back_emo, callback_data="NoDelete")])
                elif not self.search_group:
                    message_buttons.append(second_row)
            elif not self.search_group:
                message_buttons.append(second_row)

            for item in self.active_item.items:
                if self.active_item.page * NUMBER_OF_ENTRIES_ON_PAGE >= i >= self.active_item.page * NUMBER_OF_ENTRIES_ON_PAGE - NUMBER_OF_ENTRIES_ON_PAGE:
                    message_buttons.append([InlineKeyboardButton(text=str(item), callback_data=item.uuid)])
                i += 1

        return InlineKeyboardMarkup(message_buttons)

    def close(self):
        self.kdb.close()

    def open(self, chat_id, password=None, keyfile_path=None):

        self.user = DBSession.query(User).filter(User.chat_id == chat_id).first()

        try:
            with libkeepass.open(filename=self.path, password=password, keyfile=keyfile_path, unprotect=False) as kdb:
                self.kdb = kdb
                self.user.is_opened = True
                save_object(self.user)
                self._root_obj = kdb.obj_root
                # print(etree.tounicode(self._root_obj, pretty_print=True))

                self.opened = True
                self._init_root_group()
                self.root_group.activate()

        except IOError as e:
            logger.error(str(e))
            raise IOError("Master password or key-file wrong")
        except UnicodeDecodeError:
            raise IOError("Critical error, please report to administrator.")

    def get_message(self):
        if not self.opened:
            raise IOError("Database not opened")

        message_text = ""
        active_item = self.active_item

        if active_item.type == ItemType.ENTRY:
            message_text += "_______" + key_emo + active_item.name + "_______" + new_line
            i = 0
            for item in active_item.items:
                if active_item.page * NUMBER_OF_ENTRIES_ON_PAGE >= i >= active_item.page * NUMBER_OF_ENTRIES_ON_PAGE - NUMBER_OF_ENTRIES_ON_PAGE:
                    try:
                        message_text += str(item) + new_line
                    except TypeError:
                        continue
                i += 1
            if i > NUMBER_OF_ENTRIES_ON_PAGE:
                message_text += "_______Page {0} of {1}_______".format(active_item.page, int(
                    math.ceil(active_item.size / NUMBER_OF_ENTRIES_ON_PAGE))) + new_line
            else:
                message_text += "_______Page {0} of {1}_______".format(active_item.page, 1) + new_line

        if active_item.type == ItemType.GROUP:

            message_text += "_______" + str(active_item) + "_______" + new_line
            i = 0
            for item in active_item.items:
                if active_item.page * NUMBER_OF_ENTRIES_ON_PAGE >= i >= active_item.page * NUMBER_OF_ENTRIES_ON_PAGE - NUMBER_OF_ENTRIES_ON_PAGE:
                    if item.type == ItemType.ENTRY:
                        message_text += key_emo + str(item) + new_line
                    if item.type == ItemType.GROUP:
                        message_text += folder_emo + str(item) + new_line
                i += 1
            if i > NUMBER_OF_ENTRIES_ON_PAGE:
                message_text += "_______Page {0} of {1}_______".format(active_item.page, int(
                    math.ceil(active_item.size / NUMBER_OF_ENTRIES_ON_PAGE))) + new_line
            else:
                message_text += "_______Page {0} of {1}_______".format(active_item.page, 1) + new_line

        message_markup = self._generate_keyboard()

        return message_text, message_markup

    def search_item(self, word):
        if not self.opened:
            raise IOError("Databse not opened")

        finded_items = []

        def __inner_find(parent_item):
            if word.lower() in parent_item.name.lower():
                finded_items.append(parent_item)
            for item in parent_item.items:
                if item.type == ItemType.GROUP:
                    __inner_find(item)
                if word.lower() in item.name.lower():
                    finded_items.append(item)

        __inner_find(self.root_group)

        return finded_items

    def get_item_by_uuid(self, word):
        if not self.opened:
            raise IOError("Databse not opened")

        def __inner_find(parent_item):
            if parent_item.uuid == word:
                return parent_item
            for item in parent_item.items:
                if item.type == ItemType.GROUP:
                    finded_elem = __inner_find(item)
                    if finded_elem:
                        return finded_elem
                if item.uuid == word:
                    return item

        if self.search_group:
            return __inner_find(self.search_group)
        else:
            return __inner_find(self.root_group)

    def search(self, word):
        finded_items = self.search_item(word)

        self.search_group = KeeGroup(self, self.active_item, name="Search")
        for item in finded_items:
            if isinstance(item, KeeEntry):
                temp_entry = KeeEntry(self, self.search_group, AutoType(True))
                for string in item.items:
                    temp_entry.append(string)

                self.search_group.append(temp_entry)

        self.search_group.activate()

    def start_add_edit(self, item_type=None, obj=None):
        self.add_edit_state = AddEditState(item_type, obj)
        return self.add_edit_state.get_message()

    def finish_add_edit(self):
        if self.add_edit_state and self.get_user().create_state:
            process_type = self.add_edit_state.process_type

            """Get parent group"""
            group_item = self.active_item
            while not group_item.type == ItemType.GROUP:
                group_item = group_item.get_parent()

            """If we working with Entry"""
            if self.add_edit_state.type == ItemType.ENTRY:

                if process_type == ProcessType.ADD:
                    entry = KeeEntry(root=self, parent=group_item, autotype=AutoType("True"))
                    for key, value in self.add_edit_state.get_rawstrings():
                        entry.append(EntryString(root=self, parent=entry, key=key, value=value))
                    group_item.append(entry)

                elif process_type == ProcessType.EDIT:
                    entry = self.active_item
                    for key, value in self.add_edit_state.get_rawstrings():
                        entry.update_item(key, value)

            elif self.add_edit_state.type == ItemType.GROUP:
                gr_name = "None"
                for key, value in self.add_edit_state.get_rawstrings():
                    if key == "Name":
                        gr_name = value

                if process_type == ProcessType.ADD:
                    group = KeeGroup(root=self, parent=group_item, name=gr_name)
                    group_item.append(group)

                elif process_type == ProcessType.EDIT:
                    group = self.active_item
                    for key, value in self.add_edit_state.get_rawstrings():
                        if key == 'Name':
                            group.name = value
                        if key == 'Note':
                            group.notes = value

            group_item.activate()

            self.update_kdb_in_db()

        self.add_edit_state = None

    def update_kdb_in_db(self):
        self.generate_root()
        self.kdb.obj_root = self._root_obj

        # print(etree.tounicode(self._root_obj, pretty_print=True))

        """Write to new memory file"""
        output = BytesIO()
        self.kdb.write_to(output)
        output.name = self.root_group.name + '.kdbx'
        output.seek(0)

        """Saving to database"""
        user = self.get_user()
        user.file = output.getvalue()
        save_object(user)


class AddEditState:
    def __init__(self, item_type=None, obj=None):
        # Entry or Group
        if item_type != ItemType.ENTRY and item_type != ItemType.GROUP:
            if obj is not None:
                self.type = obj.type
                self.process_type = ProcessType.EDIT
            else:
                raise KeyError("Choises are (Entry, Group)")
        else:
            self.type = item_type
            self.process_type = ProcessType.ADD

        if self.type == ItemType.ENTRY:
            self.fields = {"Title": None,
                           "UserName": None,
                           "Password": None,
                           "URL": None,
                           "Notes": None}
            self.req_fields = ["Title", "Password"]
            self.current_field = "Title"
            if obj is not None:
                for field_name in self.fields:
                    self.fields[field_name] = obj.get_item(field_name).value
        if self.type == ItemType.GROUP:
            self.fields = {"Name": None,
                           "Notes": None}
            self.req_fields = ["Name", ]
            self.current_field = "Name"
            if obj is not None:
                self.fields["Name"] = obj.name
                self.fields["Notes"] = obj.notes

    def get_rawstrings(self):
        rawstrings = []
        for key, value in self.fields.items():
            rawstrings.append((key, value))

        return rawstrings

    def get_message(self):
        message_text = self._generate_text()
        message_markup = self._generate_keyboard()

        return message_text, message_markup

    def set_cur_field_value(self, value):
        self.fields[self.current_field] = value

    def set_cur_field(self, field_name):
        self.current_field = field_name

    def next_field(self):
        finded = False
        for field in self.fields.keys():
            if finded:
                self.current_field = field
                break
            if self.current_field == field:
                finded = True

    def prev_field(self):
        prev_field = list(self.fields.keys())[0]
        for field in self.fields.keys():
            if self.current_field == field:
                self.current_field = prev_field
                break

            prev_field = field

    def generate_password(self):
        from random import sample as rsample

        s = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        passlen = 8
        gen_password = "".join(rsample(s, passlen))

        prev_field = self.current_field
        self.set_cur_field("Password")
        self.set_cur_field_value(gen_password)
        self.set_cur_field(prev_field)

    def _generate_text(self):
        message_text = "_______" + str(self.type) + "_______" + new_line
        for field in self.fields.keys():
            if self.current_field == field:
                message_text += f"{arrow_right_emo}"
            else:
                message_text += "      "

            if field in self.req_fields:
                message_text += f"<b>{field}</b>: "
            else:
                message_text += f"{field}: "

            message_text += str(self.fields.get(field, "")) + new_line

        return message_text

    def _generate_keyboard(self):
        message_buttons = [[InlineKeyboardButton(text=arrow_left_emo, callback_data="create_Left"),
                            InlineKeyboardButton(text=arrow_up_emo, callback_data="create_Back"),
                            InlineKeyboardButton(text=lock_emo, callback_data="Lock"),
                            InlineKeyboardButton(text=arrow_right_emo, callback_data="create_Right")]]

        for field in self.fields.keys():
            if field == "Password":
                message_buttons.append([InlineKeyboardButton(text=field, callback_data=f"create_{field}"),
                                        InlineKeyboardButton(text="Generate",
                                                             callback_data="create_generate_password")])
            else:
                message_buttons.append([InlineKeyboardButton(text=field, callback_data=f"create_{field}")])

        message_buttons.append([InlineKeyboardButton(text="---Done---", callback_data="create_done")])

        return InlineKeyboardMarkup(message_buttons)


class KeeGroup(BaseKeePass):
    def __init__(self, root, parent, name, notes="", icond_id='37', uuid=""):
        if parent is None:
            parent = self
        super().__init__(root, parent)
        if not uuid:
            self.uuid = (base64.b64encode(uuid_generator.uuid4().bytes)).decode("utf-8")
        else:
            self.uuid = uuid

        self.type = ItemType.GROUP
        self.name = name
        self.notes = notes
        self.icon_id = int(icond_id)
        self.items = []
        self.size = 0

    def __str__(self):
        if self.name:
            return self.name
        else:
            return " "

    def append(self, item):
        """
        Adding new entry to group
        :param item: KeeEntry like object
        :return: None
        """
        if not isinstance(item, KeeEntry) and not isinstance(item, KeeGroup):
            raise TypeError("KeeGroup accepts only KeeEntry or KeeGroup items")
        self.items.append(item)
        self.size += 1

    def get_xml_element(self):
        group = Element("Group")

        uuid_el = SubElement(group, 'UUID')
        uuid_el.text = self.uuid

        name_el = SubElement(group, 'Name')
        name_el.text = self.name

        notes = SubElement(group, 'Notes')
        notes.text = ""

        icon_id = SubElement(group, 'IconID')
        icon_id.text = str(self.icon_id)

        # ------Times
        times = SubElement(group, "Times")

        nowdate = datetime.datetime.now()

        cr_time = SubElement(times, "CreationTime")
        cr_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        lm_time = SubElement(times, "LastModificationTime")
        lm_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        la_time = SubElement(times, "LastAccessTime")
        la_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        et_time = SubElement(times, "ExpiryTime")
        et_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        expires = SubElement(times, "Expires")
        expires.text = "False"

        usage_count = SubElement(times, "UsageCount")
        usage_count.text = "0"

        lch_time = SubElement(times, "LocationChanged")
        lch_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Times-------------------

        is_expanded = SubElement(group, "IsExpanded")
        is_expanded.text = 'True'

        # Autotype -----------
        default_autotype_seq = SubElement(group, "DefaultAutoTypeSequence")
        default_autotype_seq.text = ''
        enable_at = SubElement(group, "EnableAutoType")
        enable_at.text = "null"

        # Searching -----------
        enable_searching = SubElement(group, "EnableSearching")
        enable_searching.text = "null"

        last_top_vis_entry = SubElement(group, "LastTopVisibleEntry")
        last_top_vis_entry.text = "AAAAAAAAAAAAAAAAAAAAAA=="

        for item in self.items:
            group.append(item.get_xml_element())

        return group

    def delete(self):
        self.deactivate()
        self._parent.items.remove(self)
        self._root.update_kdb_in_db()


class KeeEntry(BaseKeePass):

    def __init__(self, root, parent, autotype, icond_id='0', uuid=""):
        super().__init__(root, parent)
        if not uuid:
            self.uuid = (base64.b64encode(uuid_generator.uuid4().bytes)).decode("utf-8")
        else:
            self.uuid = uuid
        self.type = ItemType.ENTRY
        self.icon_id = int(icond_id)
        self.items = []
        self.size = 0
        self.name = ""
        self.autotype = autotype

    def __str__(self):
        return self.name

    def append(self, item):
        if not isinstance(item, EntryString):
            raise TypeError("KeeEntry accepts only EntryString items")
        self.items.append(item)
        self.size += 1
        # also set name
        if item.key == "Title":
            self.name = item.value

    def update_item(self, key, value):
        if key is None:
            return
        for item in self.items:
            if item.key == key:
                item.value = value

    def get_item(self, key):
        if key is None:
            return
        for item in self.items:
            if item.key == key:
                return item

    def get_xml_element(self):
        entry = Element("Entry")

        uuid_el = SubElement(entry, 'UUID')
        uuid_el.text = self.uuid

        icon_id = SubElement(entry, 'IconID')
        icon_id.text = str(self.icon_id)

        SubElement(entry, "ForegroundColor")
        SubElement(entry, "BackgroundColor")
        SubElement(entry, "OverrideURL")
        SubElement(entry, "Tags")

        # ------Times
        times = SubElement(entry, "Times")

        nowdate = datetime.datetime.now()

        cr_time = SubElement(times, "CreationTime")
        cr_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        lm_time = SubElement(times, "LastModificationTime")
        lm_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        la_time = SubElement(times, "LastAccessTime")
        la_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        et_time = SubElement(times, "ExpiryTime")
        et_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")

        expires = SubElement(times, "Expires")
        expires.text = "False"

        usage_count = SubElement(times, "UsageCount")
        usage_count.text = "0"

        lch_time = SubElement(times, "LocationChanged")
        lch_time.text = nowdate.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Times-------------------

        # Strings ----------
        for item in self.items:
            entry.append(item.get_xml_element())

        # Autotype -----------
        entry.append(self.autotype.get_xml_element())

        SubElement(entry, "History")

        return entry

    def delete(self):
        self.deactivate()
        self._parent.items.remove(self)
        self._root.update_kdb_in_db()


class EntryString(BaseKeePass):

    def __init__(self, root, parent, key, value=None):
        super().__init__(root, parent)
        self.key = key
        self.value = value
        self.type = ItemType.STRING

        if key == "Title":
            self._parent.name = value

    def get_xml_element(self):
        string = Element('String')

        key_el = SubElement(string, 'Key')
        key_el.text = self.key

        value_el = SubElement(string, 'Value')
        value_el.text = self.value

        return string

    def __str__(self):
        if not self.value:
            return self.key + " = "
        else:
            return self.key + " = " + self.value

    def __setattr__(self, name, value):
        if hasattr(self, 'key') and self.key == "Title":
            self._parent.name = value
        super().__setattr__(name, value)


class AutoType():

    def __init__(self, enabled, dto='0', association=None):
        self.enabled = enabled
        self.dto = dto
        self.association = association
        self.type = ItemType.AUTOTYPE

    def get_xml_element(self):
        # Autotype -----------
        autotype = Element("AutoType")
        at_enabled = SubElement(autotype, "Enabled")
        at_enabled.text = self.enabled

        at_data_transfer_obfuscation = SubElement(autotype, "DataTransferObfuscation")
        at_data_transfer_obfuscation.text = self.dto

        if self.association:
            asoc = SubElement(autotype, "Association")

            window = SubElement(asoc, "Window")
            window.text = self.association['window']

            key_sec = SubElement(asoc, "KeystrokeSequence")
            key_sec.text = self.association['key_sec']

        return autotype
