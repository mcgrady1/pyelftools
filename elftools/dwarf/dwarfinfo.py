#-------------------------------------------------------------------------------
# elftools: dwarf/dwarfinfo.py
#
# DWARFInfo - Main class for accessing DWARF debug information
#
# Eli Bendersky (eliben@gmail.com)
# This code is in the public domain
#-------------------------------------------------------------------------------
from collections import namedtuple

from ..construct import CString
from ..common.exceptions import DWARFError
from ..common.utils import struct_parse, dwarf_assert
from .structs import DWARFStructs
from .compileunit import CompileUnit
from .abbrevtable import AbbrevTable


# Describes a debug section
# 
# stream: a stream object containing the data of this section
# name: section name in the container file
# global_offset: the global offset of the section in its container file
# size: the size of the section's data, in bytes
#
DebugSectionDescriptor = namedtuple('DebugSectionDescriptor', 
        'stream name global_offset size')


class DWARFInfo(object):
    """ Acts also as a "context" to other major objects, bridging between 
        various parts of the debug infromation.
    """
    def __init__(self,
            elffile,
            debug_info_sec,
            debug_abbrev_sec,
            debug_str_sec,
            debug_line_sec):
        """ stream: 
                A stream (file-like object) that contains debug sections
            
            elffile:
                ELFFile reference

            debug_*_sec:
                DebugSectionDescriptor for a section
        """
        self.elffile = elffile
        self.debug_info_sec = debug_info_sec
        self.debug_abbrev_sec = debug_abbrev_sec
        self.debug_str_sec = debug_str_sec
        self.debug_line_sec = debug_line_sec
        
        self.little_endian = self.elffile.little_endian

        # This is the DWARFStructs the context uses, so it doesn't depend on 
        # DWARF format and address_size (these are determined per CU) - set them
        # to default values.
        self.structs = DWARFStructs(
            little_endian=self.little_endian,
            dwarf_format=32,
            address_size=4)
        
        # Populate the list with CUs found in debug_info. For each CU only its
        # header is parsed immediately (the abbrev table isn't loaded before
        # it's being referenced by one of the CU's DIEs). 
        # Since there usually aren't many CUs in a single object, this
        # shouldn't present a performance problem.
        #
        self._CU = self._parse_CUs()
        
        # Cache for abbrev tables: a dict keyed by offset
        self._abbrevtable_cache = {}
    
    def num_CUs(self):
        """ Number of compile units in the debug info
        """
        return len(self._CU)
    
    def get_CU(self, n):
        """ Get the compile unit (CompileUnit object) at index #n
        """
        return self._CU[n]
    
    def iter_CUs(self):
        """ Yield all the compile units (CompileUnit objects) in the debug info
        """
        for i in range(self.num_CUs()):
            yield self.get_CU(i)
    
    def get_abbrev_table(self, offset):
        """ Get an AbbrevTable from the given offset in the debug_abbrev
            section.
            
            The only verification done on the offset is that it's within the
            bounds of the section (if not, an exception is raised).
            It is the caller's responsibility to make sure the offset actually
            points to a valid abbreviation table.
            
            AbbrevTable objects are cached internally (two calls for the same
            offset will return the same object).
        """
        dwarf_assert(
            offset < self.debug_abbrev_sec.size,
            "Offset '0x%x' to abbrev table out of section bounds" % offset)
        if offset not in self._abbrevtable_cache:
            self._abbrevtable_cache[offset] = AbbrevTable(
                structs=self.structs,
                stream=self.debug_abbrev_sec.stream,
                offset=offset)
        return self._abbrevtable_cache[offset]
    
    def get_string_from_table(self, offset):
        """ Obtain a string from the string table section, given an offset 
            relative to the section.
        """
        return struct_parse(
            CString(''),
            self.debug_str_sec.stream,
            stream_pos=offset)
    
    #------ PRIVATE ------#
    
    def _parse_CUs(self):
        """ Parse CU entries from debug_info.
        """
        offset = 0
        section_boundary = self.debug_info_sec.size
        CUlist = []
        while offset < section_boundary:
            # Section 7.4 (32-bit and 64-bit DWARF Formats) of the DWARF spec v3
            # states that the first 32-bit word of the CU header determines 
            # whether the CU is represented with 32-bit or 64-bit DWARF format.
            # 
            # So we peek at the first word in the CU header to determine its
            # dwarf format. Based on it, we then create a new DWARFStructs
            # instance suitable for this CU and use it to parse the rest.
            #
            initial_length = struct_parse(
                self.structs.Dwarf_uint32(''), self.debug_info_sec.stream, offset)
            dwarf_format = 64 if initial_length == 0xFFFFFFFF else 32
            
            # At this point we still haven't read the whole header, so we don't
            # know the address_size. Therefore, we're going to create structs
            # with a default address_size=4. If, after parsing the header, we
            # find out address_size is actually 8, we just create a new structs
            # object for this CU.
            #
            cu_structs = DWARFStructs(
                little_endian=self.little_endian,
                dwarf_format=dwarf_format,
                address_size=4)
            
            cu_header = struct_parse(
                cu_structs.Dwarf_CU_header, self.debug_info_sec.stream, offset)
            if cu_header['address_size'] == 8:
                cu_structs = DWARFStructs(
                    little_endian=self.little_endian,
                    dwarf_format=dwarf_format,
                     address_size=8)
            
            cu_die_offset = self.debug_info_sec.stream.tell()
            dwarf_assert(
                self._is_supported_version(cu_header['version']),
                "Expected supported DWARF version. Got '%s'" % cu_header['version'])
            CUlist.append(CompileUnit(
                header=cu_header,
                dwarfinfo=self,
                structs=cu_structs,
                cu_offset=offset,
                cu_die_offset=cu_die_offset))
            # Compute the offset of the next CU in the section. The unit_length
            # field of the CU header contains its size not including the length
            # field itself.
            offset = (  offset + 
                        cu_header['unit_length'] + 
                        cu_structs.initial_length_field_size())
        return CUlist
        
    def _is_supported_version(self, version):
        """ DWARF version supported by this parser
        """
        return 2 <= version <= 3

