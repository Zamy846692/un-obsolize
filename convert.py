import argparse
import os
import re
import shutil
import subprocess
import sys

class UnObsolizer(object):
    """
    Creates a list of files to parse based on command line arguments.

    This class is not required to have parsing done, it simply compiles a list
    of files to operate on, and then creates a ``FileParser`` for each
    file.

    Usage:
      unob = UnObsolizer()
      unob.get_files_from_args()
      unob.parse_files()
    """
    prompt_confirmation = True
    new_extension = None
    git_move = False
    global_function_dict = {}

    def __init__(self):
        self.files = []
        self.parsers = []

    def get_files_from_args(self):
        """
        Sets self.files to the list of files that will be operated on, based on
        the arguments given on the command line.
        """
        args = self.parse_arguments()
        UnObsolizer.prompt_confirmation = args.confirm
        UnObsolizer.new_extension = args.new_ext
        UnObsolizer.git_move = args.git_move

        if args.recurse or args.directory:
            self.append_directory_files(
                only_current_dir=not args.recurse)
        input_file_re = re.compile(args.input_re)
        self.files = [ f for f in self.files if (re.search(input_file_re, f)) ]
        self.files.extend([os.path.join(os.getcwd(), f) for f in args.files])

        for f in self.files:
            self.parsers.append(FileParser(f))

    def append_directory_files(self, only_current_dir):
        """
        Walk through directories and record the files we need to visit if the
        user specified recursive or directory mode. Appends all found files
        to 'self.files'.

        Args:
        only_current_dir (bool): do not recurse into all other directories
        """
        for (dirpath, dirnames, filenames) in os.walk(os.getcwd()):
            for f in filenames:
                self.files.append(os.path.join(dirpath, f))
            if only_current_dir:
                break;

    def parse_arguments(self):
        """
        Handle parsing arguments from the command line

        Returns:
        parser.Namespace: contains values for 'recurse', 'directory', and 'file'
          options
        """
        parser = argparse.ArgumentParser(description="Convert some C code.")
        parser.add_argument(
            '-r', dest='recurse', action='store_const', const=True,
            default=False,
            help=('Recurse and operate on all files in the current and'
                  ' sub-directories (implies -d)'))
        parser.add_argument(
            '-d', dest='directory', action='store_const', const=True,
            default=False, help='Operate on all files in the current directory')
        parser.add_argument(
            'files', metavar='file', nargs='*',
            help='Files to operate on. Optional if [-r/-d] is applied')
        parser.add_argument(
            '-xc', dest='confirm', action='store_const', const=False,
            default=True, help=('Do not prompt for confirmation before '
                                'making a change.'))
        parser.add_argument(
            '--ext', dest='new_ext', action='store', default=None,
            help=('Specify a new file extension to change the converted files '
                  'to. Useful when converting from *.c to *.cpp. '
                  'Example: [--ext cpp]'))
        parser.add_argument(
            '--re', dest='input_re', action='store', default=r'\S+\.c$',
            help=(r'Specify a Python regex to filter the files that are '
                  r'found with [-d/-r]. '
                  r'Default is `\S+\.c$`, which will usually match *.c files.'))
        parser.add_argument(
            '-gm', dest='git_move', action='store_const', const=True,
            default=False,
            help=(r'Only valid with [--ext]. '
                  r'When changing file extensions, perform a Git move. '
                  r'Useful when operating in a Git repository.'))
        return parser.parse_args()

    def parse_files(self):
        """
        Triggers the parsing process for all files in the list 'self.files'.
        First fix function declarations, then forward declarations.
        """
        for p in self.parsers:
            p.convert_func_decl()

        for p in self.parsers:
            p.convert_forward_decl()

class FileParser(object):
    """
    Represents a parser for a single file. Handles correcting and collecting
    obsolete function data.

    global function data is stored in UnObsolizer.global_function_dict
    static function data is stored in self.function_dict

    Sample Usage:
    parser = FileParser('path/to/file/to/parse')
    # This method populates the dictionaries
    parser.convert_func_decl
    # This method depends on the dictionaries
    parser.convert_forward_decl
    """

    # Parser states
    SEARCH_FOR_FUNC = 1
    READ_ARGUMENTS = 2
    REPLACE_FUNCTION = 3

    return_value_re = re.compile(
        r'^\s*(?P<static>static)?\s*(?P<retval>[a-zA-Z_][a-zA-Z0-9_]*)\s*'
        r'(?P<pointer>\*)?\s*$')
    function_name_re = re.compile(
        r'^\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*'
        r'\(\s*(?P<args>[a-zA-Z_][a-zA-Z0-9_]*'
        r'(\s*,\s*[a-zA-Z_][a-zA-Z0-9_]*)*)?\s*\)\s*$')
    function_arg_re = re.compile(
        r'^\s*(?P<type>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?P<pointer>\*)?\s*'
        r'(?P<name>\S+)\s*;\s*$')
    function_begin_re = re.compile(r'\s*\{\s*$')
    forward_declaration_re = re.compile(
        r'^\s*(?P<static>static)?\s*(extern)?\s*'
        r'(?P<type>[a-zA-Z_][a-zA-Z0-9_]*)?\s*\*?\s*'
        r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*\)\s*;\s*$')
    file_ext_re = re.compile(
        r'(?P<name>.*)\.[0-9a-zA-Z]+$')
    whitespace_re = re.compile(
        r'^\s*$')

    def __init__(self, input_file_name):
        """
        Initializes and tells the parser to operate on 'input_file_name'.
        No action is required other than initializing this class; the parsing
        process is automatically initiated.
        A backup file is saved in 'file_name.bak'; just in-case the program
        explodes.

        Args:
        input_file_name (string): the file to operate on
        """
        self.current_state = FileParser.SEARCH_FOR_FUNC
        self.function_name = ''
        self.function_args = []
        self.previous_line = ''
        self.function_args_count = 0
        self.function_dict = {}
        self.function_ret_type = ''
        self.function_is_global = False
        self.accumulated_lines = []
        self.input_file_name = input_file_name

    def convert_func_decl(self):
        """
        Read through self.input_file_name and replace all obsolete function
        declarations. Note that this method writes to a temporary file `*.tmp`
        and not to the final output file. You must call `convert_forward_decl`
        after this method to create the proper output file.

        This method creates a backup file `*.bak` in case of disaster.

        Stores all converted static functions in self.function_dict, and all
        converted global functions in UnObsolizer.global_function_dict.
        """
        # Save original file in case of disaster
        backup_file_name = '{}.bak'.format(self.input_file_name)
        self.temp_file_name = '{}.tmp'.format(self.input_file_name)
        self.output_file_name = self.input_file_name
        if UnObsolizer.new_extension:
            no_ext_name = re.search(FileParser.file_ext_re,
                                    self.input_file_name)
            if no_ext_name:
                self.output_file_name = '{}.{}'.format(
                    no_ext_name.group('name'),
                    UnObsolizer.new_extension)
        shutil.copyfile(self.input_file_name, backup_file_name)
        self.output_file = open(self.output_file_name, 'w+')
        self.operate_on_file(backup_file_name, self.function_converter)
        self.output_file.close()
        shutil.copyfile(self.output_file_name, self.temp_file_name)

    def convert_forward_decl(self):
        """
        Read through self.temp_file_name created in `convert_func_decl` and
        replace all incorrect forward declarations.
        When this method returns, the final output file will be created
        (with optional new extension or git mv).

        For static functions, this method pulls data from self.function_dict.
        For global functions, this method pulls data from
        UnObsolizer.global_function_dict
        """
        self.output_file = open(self.output_file_name, 'w+')
        self.operate_on_file(self.temp_file_name, self.declaration_converter)
        self.output_file.close()
        os.remove(self.temp_file_name)
        if UnObsolizer.new_extension:
            os.remove(self.input_file_name)
            if UnObsolizer.git_move:
                shutil.move(self.output_file_name, self.input_file_name)
                ret_val = subprocess.call(
                    ['git', 'mv', self.input_file_name, self.output_file_name])
                if ret_val:
                    print('Fatal error while performing `git mv`. '
                          'Exiting')
                    exit(1)

    def operate_on_file(self, file_name, handle):
        """
        Calls the passed in function on every line of the specified file.
        There will be two passes on the file, so this is useful to avoid
        redundant code.

        Args:
        file_name (string): name of the file to read
        handle (function(string)): the function to call for every line of the
          file (takes the line of the file as an argument)
        """
        file_ = open(file_name, 'r')
        for line in file_:
            handle(line)
            self.previous_line = line
        file_.close()

    def function_converter(self, line):
        """
        The main state handler for the function-converter.
        Passes 'line' onto the proper function depending on the current state.

        Args:
        line (string): the current string to be processing
        """
        if self.current_state is FileParser.SEARCH_FOR_FUNC:
            self.search_for_func(line)
        elif self.current_state is FileParser.READ_ARGUMENTS:
            self.read_arguments(line)
        elif self.current_state is FileParser.REPLACE_FUNCTION:
            self.replace_function(line)

    def search_for_func(self, line):
        """
        Searches 'line' for a function declaration of the form:
        '[name]([arg], [arg])'.

        Args:
        line (string): the string to search for a function in

        Modified instance variables:
        function_decl: will contain the name of the function (if one is found)
        current_state: set to the proper next state based on the # of
          arguments expected
        function_args_count: set to the expected number of arguments
        """
        func_name_match = re.search(FileParser.function_name_re, line)
        if func_name_match:
            self.accumulated_lines.append(line)

            # Grab expected number of arguments
            if func_name_match.group('args'):
                self.function_args_count = len(
                    func_name_match.group('args').split(','))

            # Use 'int' if there is no return value
            ret_value_match = re.search(FileParser.return_value_re,
                                        self.previous_line)
            if ret_value_match:
                if ret_value_match.group('static'):
                    self.function_is_global = False
                else:
                    self.function_is_global = True
            else:
                self.function_is_global = False
                self.function_ret_type = 'int\n'
            self.function_name = func_name_match.group('name')
            if self.function_args_count == 0:
                self.current_state = FileParser.REPLACE_FUNCTION
            else:
                self.current_state = FileParser.READ_ARGUMENTS
        else:
             self.output_file.write(line)
             self.reset_state()

    def read_arguments(self, line):
        """
        Reads the arguments that follow the function declaration. Stores
        them in a tuple for re-writing later.

        Args:
        line (string): the string to search for an argument in

        Modified instance variables:
        function_args: add found argument tuples to the list
        current_state: set to next state when all arguments have been found
        """
        self.accumulated_lines.append(line)
        arg_match = re.search(FileParser.function_arg_re, line)
        if arg_match:
            arg_type = arg_match.group('type')
            arg_name = arg_match.group('name')
            arg_ptr = True if arg_match.group('pointer') else False
            self.function_args.append((arg_type, arg_name, arg_ptr))
            if len(self.function_args) is self.function_args_count:
                self.current_state = FileParser.REPLACE_FUNCTION
        elif re.search(FileParser.whitespace_re, line):
            pass
        else:
            self.write_accumulator()
            self.reset_state()

    def replace_function(self, line):
        """
        Replaces the obsolete function header with the new one built from
        the function name and the argument tuples. Then resets the state
        machine.

        Args:
        line (string): the line containing the opening brace of the function
        """
        open_curley_match = re.search(FileParser.function_begin_re, line)
        if open_curley_match:
            function_declaration = self.function_name
            function_declaration += '('
            index = 1
            for arg in self.function_args:
                function_declaration += '{}{} {}'.format(
                    arg[0],
                    '*' if arg[2] else '',
                    arg[1])
                if index < len(self.function_args):
                    function_declaration += ', '
                index += 1
            function_declaration += ')\n'
            confirmation = 'y'
            if UnObsolizer.prompt_confirmation:
                print('Replace?\n')
                for al in self.accumulated_lines:
                    print(al.rstrip('\n'))
                print('--with--')
                print(function_declaration)
                confirmation = input('y/n [y]')
            if confirmation == 'n':
                self.output_file.write(self.function_ret_type)
                self.output_file.writelines(self.accumulated_lines)
            else:
                self.output_file.write(function_declaration)
                if self.function_is_global:
                    UnObsolizer.global_function_dict[self.function_name] = self.function_args
                else:
                    self.function_dict[self.function_name] = self.function_args
            self.output_file.write(line)
        elif re.search(FileParser.whitespace_re, line):
            return
        else:
            self.output_file.writelines(self.accumulated_lines)
            self.output_file.write(line)
        self.reset_state()

    def write_accumulator(self):
        """
        Writes the contents of `self.accumulated_lines` to `self.output_file`.

        `self.accumulated_lines' is not modified.
        """
        self.output_file.writelines(self.accumulated_lines)

    def reset_state(self):
        """
        Resets the state machine to its default values.
        """
        self.current_state = FileParser.SEARCH_FOR_FUNC
        self.accumulated_lines = []
        self.function_name = ''
        self.function_args = []
        self.function_ret_type = ''
        self.function_args_count = 0

    def declaration_converter(self, line):
        """
        Scans the line for a forward declaration. If an empty forward
        delcaration exists, and we have un-obsolized the function
        then we fill in the forward delcaration with the expected arguments.

        Args:
        line (string): The line to scan for a forward declaration
        """
        forward_decl_match = re.search(FileParser.forward_declaration_re, line)
        if forward_decl_match:
            # Ensure we have arguments for this forward declaration
            func_name = forward_decl_match.group('name')
            try:
                if forward_decl_match.group('static'):
                    args_tuple_list = self.function_dict[func_name]
                else:
                    args_tuple_list = UnObsolizer.global_function_dict[func_name]
            except KeyError:
                self.output_file.write(line)
                return
            new_forward_decl_args = ''
            index = 1
            for arg_tuple in args_tuple_list:
                new_forward_decl_args += arg_tuple[0]
                if arg_tuple[2]:
                    new_forward_decl_args += '* '
                else:
                    new_forward_decl_args += ' '
                new_forward_decl_args += arg_tuple[1]
                if index < len(args_tuple_list):
                    new_forward_decl_args += ', '
                index += 1
            repl = re.sub(
                '\((.*)\)',
                '({})'.format(new_forward_decl_args),
                line)
            confirmation = 'y'
            if UnObsolizer.prompt_confirmation:
                print('Replace?\n')
                print(line.rstrip('\n'))
                print('--with--')
                print(repl)
                confirmation = input('y/n [y]')
            if confirmation == 'n':
                self.output_file.write(line)
            else:
                self.output_file.write(repl)
        else:
            self.output_file.write(line)


if __name__ == '__main__':
    unob = UnObsolizer()
    unob.get_files_from_args()
    unob.parse_files()
