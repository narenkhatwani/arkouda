from __future__ import annotations

import itertools
from typing import cast, Tuple, List, Optional, Union, Dict
from typeguard import typechecked
from arkouda.client import generic_msg
from arkouda.pdarrayclass import pdarray, create_pdarray, parse_single_value, \
     unregister_pdarray_by_name, RegistrationError
from arkouda.logger import getArkoudaLogger
import numpy as np # type: ignore
from arkouda.dtypes import npstr, int_scalars, str_scalars
from arkouda.dtypes import NUMBER_FORMAT_STRINGS, resolve_scalar_dtype, \
     translate_np_dtype
import json
import re
from arkouda.infoclass import information, list_symbol_table

__all__ = ['Strings']

class Strings:
    """
    Represents an array of strings whose data resides on the
    arkouda server. The user should not call this class directly;
    rather its instances are created by other arkouda functions.

    Attributes
    ----------
    offsets : pdarray
        The starting indices for each string
    bytes : pdarray
        The raw bytes of all strings, joined by nulls
    size : int_scalars
        The number of strings in the array
    nbytes : int_scalars
        The total number of bytes in all strings
    ndim : int_scalars
        The rank of the array (currently only rank 1 arrays supported)
    shape : tuple
        The sizes of each dimension of the array
    dtype : dtype
        The dtype is ak.str
    regex_dict: Dict[str, Tuple[pdarray, pdarray, pdarray]]
        Dictionary storing information on matches (cache of Strings.find_locations(pattern))
        Keys - regex patterns
        Values - tuples of pdarrays (numMatches, matchStarts, matchLens)
    logger : ArkoudaLogger
        Used for all logging operations
        
    Notes
    -----
    Strings is composed of two pdarrays: (1) offsets, which contains the
    starting indices for each string and (2) bytes, which contains the 
    raw bytes of all strings, delimited by nulls.    
    """

    BinOps = frozenset(["==", "!="])
    objtype = "str"

    def __init__(self, offset_attrib : Union[pdarray,str], 
                 bytes_attrib : Union[pdarray,str]) -> None:
        """
        Initializes the Strings instance by setting all instance
        attributes, some of which are derived from the array parameters.
        
        Parameters
        ----------
        offset_attrib : Union[pdarray, str]
            the array containing the offsets 
        bytes_attrib : Union[pdarray, str]
            the array containing the string values    
            
        Returns
        -------
        None
        
        Raises
        ------
        RuntimeError
            Raised if there's an error converting a server-returned str-descriptor
            or pdarray to either the offset_attrib or bytes_attrib   
        ValueError
            Raised if there's an error in generating instance attributes 
            from either the offset_attrib or bytes_attrib parameter 
        """
        if isinstance(offset_attrib, pdarray):
            self.offsets = offset_attrib
        else:
            try:
                self.offsets = create_pdarray(offset_attrib)
            except Exception as e:
                raise RuntimeError(e)
        if isinstance(bytes_attrib, pdarray):
            self.bytes = bytes_attrib
        else:
            try:
                self.bytes = create_pdarray(bytes_attrib)
            except Exception as e:
                raise RuntimeError(e)
        try:
            self.size = self.offsets.size
            self.nbytes = self.bytes.size
            self.ndim = self.offsets.ndim
            self.shape = self.offsets.shape
        except Exception as e:
            raise ValueError(e)   

        self.dtype = npstr
        self.name:Optional[str] = None
        self.regex_dict: Dict = dict()
        self.logger = getArkoudaLogger(name=__class__.__name__) # type: ignore

    def __iter__(self):
        raise NotImplementedError('Strings does not support iteration. To force data transfer from server, use to_ndarray')

    def __len__(self) -> int:
        return self.shape[0]

    def __str__(self) -> str:
        from arkouda.client import pdarrayIterThresh
        if self.size <= pdarrayIterThresh:
            vals = ["'{}'".format(self[i]) for i in range(self.size)]
        else:
            vals = ["'{}'".format(self[i]) for i in range(3)]
            vals.append('... ')
            vals.extend(["'{}'".format(self[i]) for i in range(self.size-3, self.size)])
        return "[{}]".format(', '.join(vals))

    def __repr__(self) -> str:
        return "array({})".format(self.__str__())

    @typechecked
    def _binop(self, other : Union[Strings,str_scalars], op : str) -> pdarray:
        """
        Executes the requested binop on this Strings instance and the
        parameter Strings object and returns the results within
        a pdarray object.

        Parameters
        ----------
        other : Strings, str_scalars
            the other object is a Strings object
        op : str
            name of the binary operation to be performed 
      
        Returns
        -------
        pdarray
            encapsulating the results of the requested binop      

        Raises
        -----
        ValueError
            Raised if (1) the op is not in the self.BinOps set, or (2) if the
            sizes of this and the other instance don't match, or (3) the other
            object is not a Strings object
        RuntimeError
            Raised if a server-side error is thrown while executing the
            binary operation
        """
        if op not in self.BinOps:
            raise ValueError("Strings: unsupported operator: {}".format(op))
        if isinstance(other, Strings):
            if self.size != other.size:
                raise ValueError("Strings: size mismatch {} {}".\
                                 format(self.size, other.size))
            cmd = "segmentedBinopvv"
            args = "{} {} {} {} {} {} {}".format(op,
                                                 self.objtype,
                                                 self.offsets.name,
                                                 self.bytes.name,
                                                 other.objtype,
                                                 other.offsets.name,
                                                 other.bytes.name)
        elif resolve_scalar_dtype(other) == 'str':
            cmd = "segmentedBinopvs"
            args = "{} {} {} {} {} {}".format(op,
                                                              self.objtype,
                                                              self.offsets.name,
                                                              self.bytes.name,
                                                              self.objtype,
                                                              json.dumps([other]))
        else:
            raise ValueError("Strings: {} not supported between Strings and {}"\
                             .format(op, other.__class__.__name__))
        return create_pdarray(generic_msg(cmd=cmd,args=args))

    def __eq__(self, other) -> bool:
        return self._binop(other, "==")

    def __ne__(self, other) -> bool:
        return self._binop(cast(Strings, other), "!=")

    def __getitem__(self, key):
        if np.isscalar(key) and resolve_scalar_dtype(key) == 'int64':
            orig_key = key
            if key < 0:
                # Interpret negative key as offset from end of array
                key += self.size
            if (key >= 0 and key < self.size):
                cmd = "segmentedIndex"
                args = " {} {} {} {} {}".format('intIndex',
                                                self.objtype,
                                                self.offsets.name,
                                                self.bytes.name,
                                                key)
                repMsg = generic_msg(cmd=cmd,args=args)
                _, value = repMsg.split(maxsplit=1)
                return parse_single_value(value)
            else:
                raise IndexError("[int] {} is out of bounds with size {}".\
                                 format(orig_key,self.size))
        elif isinstance(key, slice):
            (start,stop,stride) = key.indices(self.size)
            self.logger.debug('start: {}; stop: {}; stride: {}'.format(start,stop,stride))
            cmd = "segmentedIndex"
            args = " {} {} {} {} {} {} {}".format('sliceIndex',
                                                  self.objtype,
                                                  self.offsets.name,
                                                  self.bytes.name,
                                                  start,
                                                  stop,
                                                  stride)
            repMsg = generic_msg(cmd=cmd, args=args)
            offsets, values = repMsg.split('+')
            return Strings(offsets, values);
        elif isinstance(key, pdarray):
            kind, _ = translate_np_dtype(key.dtype)
            if kind not in ("bool", "int"):
                raise TypeError("unsupported pdarray index type {}".format(key.dtype))
            if kind == "bool" and self.size != key.size:
                raise ValueError("size mismatch {} {}".format(self.size,key.size))
            cmd = "segmentedIndex"
            args = "{} {} {} {} {}".format('pdarrayIndex',
                                                         self.objtype,
                                                         self.offsets.name,
                                                         self.bytes.name,
                                                         key.name)
            repMsg = generic_msg(cmd=cmd,args=args)
            offsets, values = repMsg.split('+')
            return Strings(offsets, values)
        else:
            raise TypeError("unsupported pdarray index type {}".format(key.__class__.__name__))

    def get_lengths(self) -> pdarray:
        """
        Return the length of each string in the array.

        Returns
        -------
        pdarray, int
            The length of each string
            
        Raises
        ------
        RuntimeError
            Raised if there is a server-side error thrown
        """
        cmd = "segmentLengths"
        args = "{} {} {}".\
                        format(self.objtype, self.offsets.name, self.bytes.name)
        return create_pdarray(generic_msg(cmd=cmd,args=args))

    def cached_regex_patterns(self):
        """
        Returns the regex patterns for which Strings.find_locations(pattern) have been cached
        """
        sym_tab = list_symbol_table()
        self.regex_dict = {key: val for key, val in self.regex_dict.items() if
                           all([pda.name in sym_tab for pda in val])}
        return self.regex_dict.keys()

    @typechecked
    def find_locations(self, pattern: Union[bytes, str_scalars]) -> Tuple[pdarray, pdarray, pdarray]:
        """
        Finds pattern matches and returns pdarrays containing the number, start postitions, and lengths of matches
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Parameters
        ----------
        pattern: str_scalars
            The regex pattern used to find matches

        Returns
        -------
        pdarray, int64
            For each original string, the number of pattern matches
        pdarray, int64
            The start positons of pattern matches
        pdarray, int64
            The lengths of pattern matches

        Raises
        ------
        TypeError
            Raised if the pattern parameter is not bytes or str_scalars
        ValueError
            Rasied if pattern is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.findall, Strings.match

        Examples
        --------
        >>> strings = ak.array(['{} string {}'.format(i, i) for i in range(1, 6)])
        >>> strings
        array(['1 string 1', '2 string 2', '3 string 3', '4 string 4', '5 string 5'])
        >>> num_matches, starts, lens = strings.find_locations('\\d')
        >>> num_matches
        array([2, 2, 2, 2, 2])
        >>> starts
        array([0, 9, 11, 20, 22, 31, 33, 42, 44, 53])
        >>> lens
        array([1, 1, 1, 1, 1, 1, 1, 1, 1, 1]))
        """
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        sym_tab = list_symbol_table()
        if pattern not in self.regex_dict or any([pda.name not in sym_tab for pda in self.regex_dict[pattern]]):
            # run find_locations if we don't have the result of find_locations(pattern) cached or any of the references have gone stale
            try:
                re.compile(pattern)
            except Exception as e:
                raise ValueError(e)
            cmd = "segmentedFindLoc"
            args = "{} {} {} {}".format(self.objtype,
                                        self.offsets.name,
                                        self.bytes.name,
                                        json.dumps([pattern]))
            repMsg = cast(str, generic_msg(cmd=cmd, args=args))
            arrays = repMsg.split('+', maxsplit=2)
            self.regex_dict[pattern] = (create_pdarray(arrays[0]), create_pdarray(arrays[1]), create_pdarray(arrays[2]))
        return self.regex_dict[pattern]

    @typechecked
    def findall(self, pattern: Union[bytes, str_scalars], return_match_origins: bool = False) -> Union[Strings, Tuple]:
        """
        Return all non-overlapping matches of pattern in Strings as a new Strings object
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Parameters
        ----------
        pattern: str_scalars
            The regex pattern used to find matches
        return_match_origins: bool
            If True, return a pdarray containing the index of the original string each pattern match is from

        Returns
        -------
        Strings
            Strings object containing only pattern matches
        pdarray, int64 (optional)
            The index of the original string each pattern match is from

        Raises
        ------
        TypeError
            Raised if the pattern parameter is not bytes or str_scalars
        ValueError
            Rasied if pattern is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.find_locations, Strings.match

        Examples
        --------
        >>> strings = ak.array(['{} string {}'.format(i, i) for i in range(1, 6)])
        >>> strings
        array(['1 string 1', '2 string 2', '3 string 3', '4 string 4', '5 string 5'])
        >>> matches, match_origins = strings.findall('\\d', return_match_origins = True)
        >>> matches
        array(['1', '1', '2', '2', '3', '3', '4', '4', '5', '5'])
        >>> match_origins
        array([0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        """
        num_matches, starts, lens = self.find_locations(pattern)
        cmd = "segmentedFindAll"
        args = "{} {} {} {} {} {} {}".format(self.objtype,
                                             self.offsets.name,
                                             self.bytes.name,
                                             num_matches.name,
                                             starts.name,
                                             lens.name,
                                             return_match_origins)
        repMsg = cast(str, generic_msg(cmd=cmd, args=args))
        if return_match_origins:
            arrays = repMsg.split('+', maxsplit=2)
            return Strings(arrays[0], arrays[1]), create_pdarray(arrays[2])
        else:
            arrays = repMsg.split('+', maxsplit=1)
            return Strings(arrays[0], arrays[1])

    @typechecked
    def contains(self, substr: Union[bytes, str_scalars], regex: bool = False) -> pdarray:
        """
        Check whether each element contains the given substring.

        Parameters
        ----------
        substr: str_scalars
            The substring in the form of string or byte array to search for
        regex: bool
            Indicates whether substr is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        pdarray, bool
            True for elements that contain substr, False otherwise

        Raises
        ------
        TypeError
            Raised if the substr parameter is not bytes or str_scalars
        ValueError
            Rasied if substr is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.startswith, Strings.endswith
        
        Examples
        --------
        >>> strings = ak.array(['{} string {}'.format(i, i) for i in range(1, 6)])
        >>> strings
        array(['1 string 1', '2 string 2', '3 string 3', '4 string 4', '5 string 5'])
        >>> strings.contains('string')
        array([True, True, True, True, True])
        >>> strings.contains('string \\d', regex=True)
        array([True, True, True, True, True])
        """
        if isinstance(substr, bytes):
            substr = substr.decode()
        if regex:
            try:
                re.compile(substr)
            except Exception as e:
                raise ValueError(e)
        cmd = "segmentedEfunc"
        args = "{} {} {} {} {} {} {}".format("contains",
                                             self.objtype,
                                             self.offsets.name,
                                             self.bytes.name,
                                             "str",
                                             regex,
                                             json.dumps([substr]))
        return create_pdarray(generic_msg(cmd=cmd, args=args))

    @typechecked
    def startswith(self, substr: Union[bytes, str_scalars], regex: bool = False) -> pdarray:
        """
        Check whether each element starts with the given substring.

        Parameters
        ----------
        substr: Union[bytes, str_scalars]
            The prefix to search for
        regex: bool
            Indicates whether substr is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        pdarray, bool
            True for elements that start with substr, False otherwise

        Raises
        ------
        TypeError
            Raised if the substr parameter is not a bytes ior str_scalars
        ValueError
            Rasied if substr is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.contains, Strings.endswith
        
        Examples
        --------
        >>> strings_end = ak.array(['string {}'.format(i) for i in range(1, 6)])
        >>> strings_end
        array(['string 1', 'string 2', 'string 3', 'string 4', 'string 5'])
        >>> strings_end.startswith('string')
        array([True, True, True, True, True])
        >>> strings_start = ak.array(['{} string'.format(i) for i in range(1,6)])
        >>> strings_start
        array(['1 string', '2 string', '3 string', '4 string', '5 string'])
        >>> strings_start.startswith('\\d str', regex = True)
        array([True, True, True, True, True])
        """
        if isinstance(substr, bytes):
            substr = substr.decode()
        if regex:
            try:
                re.compile(substr)
            except Exception as e:
                raise ValueError(e)
        cmd = "segmentedEfunc"
        args = "{} {} {} {} {} {} {}".format("startswith",
                                             self.objtype,
                                             self.offsets.name,
                                             self.bytes.name,
                                             "str",
                                             regex,
                                             json.dumps([substr]))
        return create_pdarray(generic_msg(cmd=cmd, args=args))

    @typechecked
    def endswith(self, substr: Union[bytes, str_scalars], regex: bool = False) -> pdarray:
        """
        Check whether each element ends with the given substring.

        Parameters
        ----------
        substr: Union[bytes, str_scalars]
            The suffix to search for
        regex: bool
            Indicates whether substr is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        pdarray, bool
            True for elements that end with substr, False otherwise

        Raises
        ------
        TypeError
            Raised if the substr parameter is not bytes or str_scalars
        ValueError
            Rasied if substr is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.contains, Strings.startswith
        
        Examples
        --------
        >>> strings_start = ak.array(['{} string'.format(i) for i in range(1,6)])
        >>> strings_start
        array(['1 string', '2 string', '3 string', '4 string', '5 string'])
        >>> strings_start.endswith('ing')
        array([True, True, True, True, True])
        >>> strings_end = ak.array(['string {}'.format(i) for i in range(1, 6)])
        >>> strings_end
        array(['string 1', 'string 2', 'string 3', 'string 4', 'string 5'])
        >>> strings_end.endswith('ing \\d', regex = True)
        array([True, True, True, True, True])
        """
        if isinstance(substr, bytes):
            substr = substr.decode()
        if regex:
            try:
                re.compile(substr)
            except Exception as e:
                raise ValueError(e)
        cmd = "segmentedEfunc"
        args = "{} {} {} {} {} {} {}".format("endswith",
                                             self.objtype,
                                             self.offsets.name,
                                             self.bytes.name,
                                             "str",
                                             regex,
                                             json.dumps([substr]))
        return create_pdarray(generic_msg(cmd=cmd, args=args))

    @typechecked
    def match(self, pattern: Union[bytes, str_scalars]) -> pdarray:
        """
        For each element check whether the entire element matches the given regex, pattern.

        Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Parameters
        ----------
        pattern: str_scalars
            The regex in the form of string or byte array to search for

        Returns
        -------
        pdarray, bool
            True for elements that match pattern, False otherwise

        Raises
        ------
        TypeError
            Raised if the pattern parameter is not bytes or str_scalars
        ValueError
            Rasied if pattern is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        Strings.contains, Strings.startswith, Strings.endswith

        Examples
        --------
        >>> strings = ak.array(['{} string {}'.format(i, i) for i in range(1, 6)])
        >>> strings
        array(['1 string 1', '2 string 2', '3 string 3', '4 string 4', '5 string 5'])
        >>> strings.match('\\d string \\d')
        array([True, True, True, True, True])
        >>> strings.match('ing \\d')
        array([False, False, False, False, False])
        """
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        try:
            re.compile(pattern)
        except Exception as e:
            raise ValueError(e)
        cmd = "segmentedEfunc"
        args = "{} {} {} {} {} {} {}".format("match",
                                             self.objtype,
                                             self.offsets.name,
                                             self.bytes.name,
                                             "str",
                                             True,  # regex flag is always True for match
                                             json.dumps([pattern]))
        return create_pdarray(generic_msg(cmd=cmd, args=args))

    def flatten(self, delimiter: str, return_segments: bool = False, regex: bool = False) -> Union[Strings, Tuple]:
        """Unpack delimiter-joined substrings into a flat array.

        Parameters
        ----------
        delimiter: str
            Characters used to split strings into substrings
        return_segments: bool
            If True, also return mapping of original strings to first substring
            in return array.
        regex: bool
            Indicates whether delimiter is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        Strings
            Flattened substrings with delimiters removed
        pdarray, int64 (optional)
            For each original string, the index of first corresponding substring
            in the return array

        See Also
        --------
        peel, rpeel

        Examples
        --------
        >>> orig = ak.array(['one|two', 'three|four|five', 'six'])
        >>> orig.flatten('|')
        array(['one', 'two', 'three', 'four', 'five', 'six'])
        >>> flat, map = orig.flatten('|', return_segments=True)
        >>> map
        array([0, 2, 5])
        >>> under = ak.array(['one_two', 'three_____four____five', 'six'])
        >>> under_flat, under_map = under.flatten('_+', return_segments=True, regex=True)
        >>> under_flat
        array(['one', 'two', 'three', 'four', 'five', 'six'])
        >>> under_map
        array([0, 2, 5])
        """
        if regex:
            try:
                re.compile(delimiter)
            except Exception as e:
                raise ValueError(e)
        cmd = "segmentedFlatten"
        args = "{}+{} {} {} {} {}".format(self.offsets.name,
                                          self.bytes.name,
                                          self.objtype,
                                          return_segments,
                                          regex,
                                          json.dumps([delimiter]))
        repMsg = cast(str, generic_msg(cmd=cmd, args=args))
        if return_segments:
            arrays = repMsg.split('+', maxsplit=2)
            return Strings(arrays[0], arrays[1]), create_pdarray(arrays[2])
        else:
            arrays = repMsg.split('+', maxsplit=1)
            return Strings(arrays[0], arrays[1])
    
    @typechecked
    def peel(self, delimiter: Union[bytes, str_scalars], times: int_scalars = 1,
             includeDelimiter: bool = False, keepPartial: bool = False,
             fromRight: bool = False, regex: bool = False) -> Tuple:
        """
        Peel off one or more delimited fields from each string (similar 
        to string.partition), returning two new arrays of strings.
        *Warning*: This function is experimental and not guaranteed to work.

        Parameters
        ----------
        delimiter: Union[bytes, str_scalars]
            The separator where the split will occur
        times: Union[int, np.int64]
            The number of times the delimiter is sought, i.e. skip over 
            the first (times-1) delimiters
        includeDelimiter: bool
            If true, append the delimiter to the end of the first return 
            array. By default, it is prepended to the beginning of the 
            second return array.
        keepPartial: bool
            If true, a string that does not contain <times> instances of 
            the delimiter will be returned in the first array. By default, 
            such strings are returned in the second array.
        fromRight: bool
            If true, peel from the right instead of the left (see also rpeel)
        regex: bool
            Indicates whether delimiter is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        Tuple[Strings, Strings]
            left: Strings
                The field(s) peeled from the end of each string (unless 
                fromRight is true)
            right: Strings
                The remainder of each string after peeling (unless fromRight 
                is true)
 
        Raises
        ------
        TypeError
            Raised if the delimiter parameter is not byte or str_scalars, if
            times is not int64, or if includeDelimiter, keepPartial, or 
            fromRight is not bool
        ValueError
            Raised if times is < 1 or if delimiter is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown
        
        See Also
        --------
        rpeel, stick, lstick

        Examples
        --------
        >>> s = ak.array(['a.b', 'c.d', 'e.f.g'])
        >>> s.peel('.')
        (array(['a', 'c', 'e']), array(['b', 'd', 'f.g']))
        >>> s.peel('.', includeDelimiter=True)
        (array(['a.', 'c.', 'e.']), array(['b', 'd', 'f.g']))
        >>> s.peel('.', times=2)
        (array(['', '', 'e.f']), array(['a.b', 'c.d', 'g']))
        >>> s.peel('.', times=2, keepPartial=True)
        (array(['a.b', 'c.d', 'e.f']), array(['', '', 'g']))
        """
        if isinstance(delimiter, bytes):
            delimiter = delimiter.decode()
        if regex:
            try:
                re.compile(delimiter)
            except Exception as e:
                raise ValueError(e)
        if times < 1:
            raise ValueError("times must be >= 1")
        cmd = "segmentedPeel"
        args = "{} {} {} {} {} {} {} {} {} {} {}".format("peel",
                                                         self.objtype,
                                                         self.offsets.name,
                                                         self.bytes.name,
                                                         "str",
                                                         NUMBER_FORMAT_STRINGS['int64'].format(times),
                                                         NUMBER_FORMAT_STRINGS['bool'].format(includeDelimiter),
                                                         NUMBER_FORMAT_STRINGS['bool'].format(keepPartial),
                                                         NUMBER_FORMAT_STRINGS['bool'].format(not fromRight),
                                                         NUMBER_FORMAT_STRINGS['bool'].format(regex),
                                                         json.dumps([delimiter]))
        repMsg = generic_msg(cmd=cmd, args=args)
        arrays = cast(str, repMsg).split('+', maxsplit=3)
        leftStr = Strings(arrays[0], arrays[1])
        rightStr = Strings(arrays[2], arrays[3])
        return leftStr, rightStr

    def rpeel(self, delimiter: Union[bytes, str_scalars], times: int_scalars = 1,
              includeDelimiter: bool = False, keepPartial: bool = False, regex: bool = False):
        """
        Peel off one or more delimited fields from the end of each string 
        (similar to string.rpartition), returning two new arrays of strings.
        *Warning*: This function is experimental and not guaranteed to work.

        Parameters
        ----------
        delimiter: Union[bytes, str_scalars]
            The separator where the split will occur
        times: Union[int, np.int64]
            The number of times the delimiter is sought, i.e. skip over 
            the last (times-1) delimiters
        includeDelimiter: bool
            If true, prepend the delimiter to the start of the first return 
            array. By default, it is appended to the end of the 
            second return array.
        keepPartial: bool
            If true, a string that does not contain <times> instances of 
            the delimiter will be returned in the second array. By default, 
            such strings are returned in the first array.
        regex: bool
            Indicates whether delimiter is a regular expression
            Note: only handles regular expressions supported by re2 (does not support lookaheads/lookbehinds)

        Returns
        -------
        Tuple[Strings, Strings]
            left: Strings
                The remainder of the string after peeling
            right: Strings
                The field(s) that were peeled from the right of each string

        Raises
        ------
        TypeError
            Raised if the delimiter parameter is not bytes or str_scalars or
            if times is not int64
        ValueError
            Raised if times is < 1 or if delimiter is not a valid regex
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        peel, stick, lstick

        Examples
        --------
        >>> s = ak.array(['a.b', 'c.d', 'e.f.g'])
        >>> s.rpeel('.')
        (array(['a', 'c', 'e.f']), array(['b', 'd', 'g']))
        # Compared against peel
        >>> s.peel('.')
        (array(['a', 'c', 'e']), array(['b', 'd', 'f.g']))
        """
        return self.peel(delimiter, times=times, includeDelimiter=includeDelimiter, 
                         keepPartial=keepPartial, fromRight=True, regex=regex)

    @typechecked
    def stick(self, other : Strings, delimiter : Union[bytes,str_scalars] ="", 
                                        toLeft : bool=False) -> Strings:
        """
        Join the strings from another array onto one end of the strings 
        of this array, optionally inserting a delimiter.
        *Warning*: This function is experimental and not guaranteed to work.

        Parameters
        ----------
        other : Strings
            The strings to join onto self's strings
        delimiter : str
            String inserted between self and other
        toLeft : bool
            If true, join other strings to the left of self. By default,
            other is joined to the right of self.

        Returns
        -------
        Strings
            The array of joined strings

        Raises
        ------
        TypeError
            Raised if the delimiter parameter is not bytes or str_scalars
            or if the other parameter is not a Strings instance
        ValueError
            Raised if times is < 1
        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        lstick, peel, rpeel

        Examples
        --------
        >>> s = ak.array(['a', 'c', 'e'])
        >>> t = ak.array(['b', 'd', 'f'])
        >>> s.stick(t, delimiter='.')
        array(['a.b', 'c.d', 'e.f'])
        """
        if isinstance(delimiter, bytes):
            delimiter = delimiter.decode()
        cmd = "segmentedBinopvv"
        args = "{} {} {} {} {} {} {} {} {}".\
                            format("stick",
                            self.objtype,
                            self.offsets.name,
                            self.bytes.name,
                            other.objtype,
                            other.offsets.name,
                            other.bytes.name,
                            NUMBER_FORMAT_STRINGS['bool'].format(toLeft),
                            json.dumps([delimiter]))
        repMsg = generic_msg(cmd=cmd,args=args)
        return Strings(*cast(str,repMsg).split('+'))

    def __add__(self, other : Strings) -> Strings:
        return self.stick(other)

    def lstick(self, other : Strings, delimiter : Union[bytes,str_scalars] ="") -> Strings:
        """
        Join the strings from another array onto the left of the strings 
        of this array, optionally inserting a delimiter.
        *Warning*: This function is experimental and not guaranteed to work.

        Parameters
        ----------
        other : Strings
            The strings to join onto self's strings
        delimiter : Union[bytes,str_scalars]
            String inserted between self and other

        Returns
        -------
        Strings
            The array of joined strings, as other + self

        Raises
        ------
        TypeError
            Raised if the delimiter parameter is neither bytes nor a str
            or if the other parameter is not a Strings instance

        RuntimeError
            Raised if there is a server-side error thrown

        See Also
        --------
        stick, peel, rpeel

        Examples
        --------
        >>> s = ak.array(['a', 'c', 'e'])
        >>> t = ak.array(['b', 'd', 'f'])
        >>> s.lstick(t, delimiter='.')
        array(['b.a', 'd.c', 'f.e'])
        """
        return self.stick(other, delimiter=delimiter, toLeft=True)

    def __radd__(self, other : Strings) -> Strings:
        return self.lstick(other)
    
    def hash(self) -> Tuple[pdarray,pdarray]:
        """
        Compute a 128-bit hash of each string.

        Returns
        -------
        Tuple[pdarray,pdarray]
            A tuple of two int64 pdarrays. The ith hash value is the concatenation
            of the ith values from each array.

        Notes
        -----
        The implementation uses SipHash128, a fast and balanced hash function (used
        by Python for dictionaries and sets). For realistic numbers of strings (up
        to about 10**15), the probability of a collision between two 128-bit hash
        values is negligible.
        """
        cmd = "segmentedHash"
        args = "{} {} {}".format(self.objtype, self.offsets.name, 
                                              self.bytes.name)
        repMsg = generic_msg(cmd=cmd,args=args)
        h1, h2 = cast(str,repMsg).split('+')
        return create_pdarray(h1), create_pdarray(h2)

    def group(self) -> pdarray:
        """
        Return the permutation that groups the array, placing equivalent
        strings together. All instances of the same string are guaranteed to lie
        in one contiguous block of the permuted array, but the blocks are not
        necessarily ordered.

        Returns
        -------
        pdarray
            The permutation that groups the array by value

        See Also
        --------
        GroupBy, unique

        Notes
        -----
        If the arkouda server is compiled with "-sSegmentedArray.useHash=true",
        then arkouda uses 128-bit hash values to group strings, rather than sorting
        the strings directly. This method is fast, but the resulting permutation
        merely groups equivalent strings and does not sort them. If the "useHash"
        parameter is false, then a full sort is performed.
        
        Raises
        ------  
        RuntimeError
            Raised if there is a server-side error in executing group request or
            creating the pdarray encapsulating the return message
        """
        cmd = "segmentedGroup"
        args = "{} {} {}".\
                           format(self.objtype, self.offsets.name, self.bytes.name)
        return create_pdarray(generic_msg(cmd=cmd,args=args))

    def to_ndarray(self) -> np.ndarray:
        """
        Convert the array to a np.ndarray, transferring array data from the
        arkouda server to Python. If the array exceeds a built-in size limit,
        a RuntimeError is raised.

        Returns
        -------
        np.ndarray
            A numpy ndarray with the same strings as this array

        Notes
        -----
        The number of bytes in the array cannot exceed ``arkouda.maxTransferBytes``,
        otherwise a ``RuntimeError`` will be raised. This is to protect the user
        from overflowing the memory of the system on which the Python client
        is running, under the assumption that the server is running on a
        distributed system with much more memory than the client. The user
        may override this limit by setting ak.maxTransferBytes to a larger
        value, but proceed with caution.

        See Also
        --------
        array

        Examples
        --------
        >>> a = ak.array(["hello", "my", "world"])
        >>> a.to_ndarray()
        array(['hello', 'my', 'world'], dtype='<U5')
        >>> type(a.to_ndarray())
        numpy.ndarray
        """
        # Get offsets and append total bytes for length calculation
        npoffsets = np.hstack((self.offsets.to_ndarray(), np.array([self.nbytes])))
        # Get contents of strings (will error if too large)
        npvalues = self.bytes.to_ndarray()
        # Compute lengths, discounting null terminators
        lengths = np.diff(npoffsets) - 1
        # Numpy dtype is based on max string length
        dt = '<U{}'.format(lengths.max())
        res = np.empty(self.size, dtype=dt)
        # Form a string from each segment and store in numpy array
        for i, (o, l) in enumerate(zip(npoffsets, lengths)):
            res[i] = np.str_(''.join(chr(b) for b in npvalues[o:o+l]))
        return res

    @typechecked
    def save(self, prefix_path : str, dataset : str='strings_array', 
             mode : str='truncate', save_offsets : bool = True) -> str:
        """
        Save the Strings object to HDF5. The result is a collection of HDF5 files,
        one file per locale of the arkouda server, where each filename starts
        with prefix_path. Each locale saves its chunk of the Strings array to its
        corresponding file.

        Parameters
        ----------
        prefix_path : str
            Directory and filename prefix that all output files share
        dataset : str
            The name of the Strings dataset to be written, defaults to strings_array
        mode : str {'truncate' | 'append'}
            By default, truncate (overwrite) output files, if they exist.
            If 'append', create a new Strings dataset within existing files.
        save_offsets : bool
            Defaults to True which will instruct the server to save the offsets array to HDF5
            If False the offsets array will not be save and will be derived from the string values
            upon load/read.

        Returns
        -------
        String message indicating result of save operation

        Raises
        ------
        ValueError 
            Raised if the lengths of columns and values differ, or the mode is 
            neither 'truncate' nor 'append'
        TypeError
            Raised if prefix_path, dataset, or mode is not a str

        See Also
        --------
        pdarrayIO.save

        Notes
        -----
        Important implementation notes: (1) Strings state is saved as two datasets
        within an hdf5 group: one for the string characters and one for the
        segments corresponding to the start of each string, (2) the hdf5 group is named 
        via the dataset parameter. 
        """       
        if mode.lower() in 'append':
            m = 1
        elif mode.lower() in 'truncate':
            m = 0
        else:
            raise ValueError("Allowed modes are 'truncate' and 'append'")

        try:
            json_array = json.dumps([prefix_path])
        except Exception as e:
            raise ValueError(e)

        cmd = "tohdf"
        args = f"{self.bytes.name} {dataset} {m} {json_array} {self.dtype} {self.offsets.name} {save_offsets}"
        return cast(str, generic_msg(cmd, args))

    def is_registered(self) -> np.bool_:
        """
        Return True iff the object is contained in the registry

        Parameters
        ----------
        None

        Returns
        -------
        bool
            Indicates if the object is contained in the registry

        Raises
        ------
        RuntimeError
            Raised if there's a server-side error thrown
        """
        parts_registered = [np.bool_(self.offsets.is_registered()), self.bytes.is_registered()]
        if np.any(parts_registered) and not np.all(parts_registered):  # test for error
            raise RegistrationError(f"Not all registerable components of Strings {self.name} are registered.")

        return np.bool_(np.any(parts_registered))

    def _list_component_names(self) -> List[str]:
        """
        Internal Function that returns a list of all component names

        Parameters
        ----------
        None

        Returns
        -------
        List[str]
            List of all component names
        """
        return list(itertools.chain.from_iterable([self.offsets._list_component_names(), self.bytes._list_component_names()]))

    def info(self) -> str:
        """
        Returns a JSON formatted string containing information about all components of self

        Parameters
        ----------
        None

        Returns
        -------
        str
            JSON string containing information about all components of self
        """
        return information(self._list_component_names())

    def pretty_print_info(self) -> None:
        """
        Prints information about all components of self in a human readable format

        Parameters
        ----------
        None

        Returns
        -------
        None
        """
        self.offsets.pretty_print_info()
        self.bytes.pretty_print_info()

    @typechecked
    def register(self, user_defined_name: str) -> Strings:
        """
        Register this Strings object with a user defined name in the arkouda server
        so it can be attached to later using Strings.attach()
        This is an in-place operation, registering a Strings object more than once will
        update the name in the registry and remove the previously registered name.
        A name can only be registered to one object at a time.

        Parameters
        ----------
        user_defined_name : str
            user defined name which the Strings object is to be registered under

        Returns
        -------
        Strings
            The same Strings object which is now registered with the arkouda server and has an updated name.
            This is an in-place modification, the original is returned to support a fluid programming style.
            Please note you cannot register two different objects with the same name.

        Raises
        ------
        TypeError
            Raised if user_defined_name is not a str
        RegistrationError
            If the server was unable to register the Strings object with the user_defined_name
            If the user is attempting to register more than one object with the same name, the former should be
            unregistered first to free up the registration name.

        See also
        --------
        attach, unregister

        Notes
        -----
        Registered names/Strings objects in the server are immune to deletion
        until they are unregistered.
        """
        self.offsets.register(f"{user_defined_name}.offsets")
        self.bytes.register(f"{user_defined_name}.bytes")
        self.name = user_defined_name
        return self

    def unregister(self) -> None:
        """
        Unregister a Strings object in the arkouda server which was previously
        registered using register() and/or attached to using attach()

        Parameters
        ----------

        Returns
        -------
        None

        Raises
        ------
        RuntimeError
            Raised if the server could not find the internal name/symbol to remove

        See also
        --------
        register, attach

        Notes
        -----
        Registered names/Strings objects in the server are immune to deletion until
        they are unregistered.
        """
        self.offsets.unregister()
        self.bytes.unregister()
        self.name = None

    @staticmethod
    @typechecked
    def attach(user_defined_name: str) -> Strings:
        """
        class method to return a Strings object attached to the registered name in the arkouda
        server which was registered using register()

        Parameters
        ----------
        user_defined_name : str
            user defined name which the Strings object was registered under

        Returns
        -------
        Strings object
            the Strings object registered with user_defined_name in the arkouda server

        Raises
        ------
        TypeError
            Raised if user_defined_name is not a str

        See also
        --------
        register, unregister

        Notes
        -----
        Registered names/Strings objects in the server are immune to deletion
        until they are unregistered.
        """
        s = Strings(pdarray.attach(f"{user_defined_name}.offsets"),
                    pdarray.attach(f"{user_defined_name}.bytes"))
        s.name = user_defined_name
        return s

    @staticmethod
    @typechecked
    def unregister_strings_by_name(user_defined_name : str) -> None:
        """
        Unregister a Strings object in the arkouda server previously registered via register()

        Parameters
        ----------
        user_defined_name : str
            The registered name of the Strings object

        See also
        --------
        register, unregister, attach, is_registered
        """
        unregister_pdarray_by_name(f"{user_defined_name}.bytes")
        unregister_pdarray_by_name(f"{user_defined_name}.offsets")
