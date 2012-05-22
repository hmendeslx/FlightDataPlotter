'''
This application plots LFL parameters and is intended to provide quick feedback
to people altering LFL definitions.

Tkinter seems to have issues displaying windows from separate threads and
also behaves differently on Windows and Linux. Potential fixes:
* Ensure all windows are created by the main thread. Signals will have to
  be passed between threads for messages which provide feedback on processing.
* Use a separate process instead of thread for processing.
Testing will need to occur on Windows.
'''

import argparse
import configobj
import itertools
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pylab import setp
import os
import sys
import tempfile
import threading
import time
import tkMessageBox
import Tkinter

from datetime import datetime
from tkFileDialog import askopenfilename

from analysis_engine.library import align

from compass.compass_cli import parse_lfl
from compass.hdf import create_hdf

from hdfaccess.file import hdf_file


# Argument parsing.
################################################################################

def create_parser(paths):
    parser = argparse.ArgumentParser(description='Plot parameters when an LFL file changes.')
    if paths:
        parser.add_argument('lfl_path', help='Path of LFL file.')
        parser.add_argument('data_path', help='Path of raw data file.')
    parser.add_argument('-o', '--output-path', dest='output_path',
                        action='store',
                        help='Output file path (default will be a temporary location).')
    parser.add_argument('-c', dest='cli',
                        action='store_true',
                        help='Use command line arguments rather than file dialogs.')
    help_message = "Number of superframes stored in memory before writing " \
        "to HDF5 file. A value of 0 will cause all superframes to be " \
        "stored in memory. Default is 100 superframes."
    parser.add_argument('--superframes-in-memory', dest='superframes_in_memory',
                        action='store', type=int, default=-1, help=help_message)
    parser.add_argument('-f', '--frame-doubled', dest='frame_doubled',
                        default=False, action='store_true',
                        help="The input raw data is frame doubled.")
    help_message = "Plot parameters which have changed since the last processing."
    parser.add_argument('--plot-changed', dest='plot_changed',
                        default=False, action='store_true',
                        help=help_message)
    return parser
    
    
def validate_args(lfl_path, data_path, args):
    '''
    Validate arguments provided to argparse.
    '''
    if not os.path.isfile(lfl_path):
        print 'LFL path does not exist.'
        sys.exit(1)
    if not os.path.isfile(data_path):
        print 'Raw data file path does not exist.'
        sys.exit(1)
    
    if args.output_path:
        output_path = args.output_path
    else:
        output_path = tempfile.mkstemp()[1]
    
    if args.superframes_in_memory == 0 or args.superframes_in_memory < -1:
        print 'Superframes in memory argument must be -1 or positive.'
        sys.exit(1)
    
    return (
        lfl_path,
        data_path,
        output_path,
        args.superframes_in_memory,
        args.frame_doubled,
        args.plot_changed,
    )


# Processing and plotting functions
################################################################################


def plot_parameters(hdf_path, axes):
    '''
    Plot resulting parameters.
    '''
    print 'Plotting parameters.'
    params = {}
    max_freq = 0
    min_freq = float('inf')
    with hdf_file(hdf_path) as hdf:
        for param_name, param in hdf.iteritems():
            params[param_name] = param
            max_freq = max(max_freq, param.frequency)
            min_freq = min(min_freq, param.frequency)
            
    for param_name, param in params.iteritems():
        if max_freq == param.frequency:
            param_max_freq = param
        if param.frequency == min_freq:
            param_min_freq_len = len(param.array)
    
    # Truncate parameter arrays to successfully align them since the file
    # has not been through split sections.
    for param_name, param in params.iteritems():
        array_len = param_min_freq_len * (param.frequency / min_freq)
        if array_len != len(param.array):
            print 'Truncated %s from %d to %d' % (param_name, len(param.array),
                                                  array_len)
            param.array = param.array[:array_len]
    
    #===========================================================================
    # Plot Preparation
    #===========================================================================
    
    # Invariant parameters are identified here. They could be inserted into
    # the plot configuration file, but this is more straightforward.
    plt.rc('axes', grid=True)
    plt.rc('grid', color='0.75', linestyle='-', linewidth=0.5)

    # These items are altered during the plot, so not suited to plt.rc setup
    axescolor  = 'white' # Was '#fafafa'
    axprops = dict(axisbg=axescolor)
    prop = fm.FontProperties(size=10)
    legendprops = dict(shadow=True, fancybox=True, markerscale=0.5, prop=prop)
    lineprops = dict(linewidth=0.5, color='black')


    # Start by making a big clean canvas
    fig = plt.figure(facecolor='white', figsize=(20,10))
    
    # Add the "reference" altitude plot, and title this
    # (If we title the empty plot, it acquires default 0-1 scales)
    param_name = axes[1][0]
    param = params[param_name]
    array = align(param, param_max_freq)
    first_axis = fig.add_subplot(len(axes), 1, 1)
    first_axis.plot(array, label=param_name)
    plt.title("Processed on %s" % 
              datetime.strftime(datetime.now(),'%A, %d %B %Y at %X'))    
    setp(first_axis.get_xticklabels(), visible=False)
    
    # Now plot the additional data from the AXIS_N lists at the top of the lfl
    for index, param_names in axes.iteritems():
        if index == 1:
            continue
        axis = fig.add_subplot(len(axes), 1, index, sharex=first_axis)
        for param_name in param_names:
            param = params[param_name]
            # Data is aligned in time but the samples are not interpolated so 
            # that scaling issues can be easily addressed
            array = align(param, param_max_freq, data_type='non-aligned')
            if param.units == None:
                label_text = param_name + " [No units]"
            else:
                label_text = param_name + " : " + param.units
            axis.plot(array, label=label_text)
            axis.legend(loc='upper right', **legendprops)
            if index<len(axes):
                setp(axis.get_xticklabels(), visible=False)
        plt.legend(prop={'size':10})
        
    plt.show()


# Processing and plotting loops
################################################################################

class ProcessError(Exception):
    pass

class ProcessAndPlotLoops(threading.Thread):
    def __init__(self, hdf_path, plot_changed):
        '''
        :param hdf_path: Output path for HDF file.
        :type hdf_path: str
        '''
        self._hdf_path = hdf_path
        
        self._changed_params = set()
        self._plot_changed = plot_changed
        
        self.__error_lock = threading.Lock()
        self.__error_messages = []
        
        self.exit_loop = threading.Event()
        self._ready_to_plot = threading.Event()
        
        self._axes = None
        
        self._last_config = None
        
        super(ProcessAndPlotLoops, self).__init__()
    
    def _queue_error_message(self, title, message):
        self.__error_lock.acquire()
        self.__error_messages.append((title, message))
        self.__error_lock.release()
    
    def _get_error_message(self):
        self.__error_lock.acquire()
        if self.__error_messages:
            message = self.__error_messages.pop()
        else:
            message = None
        self.__error_lock.release()
        return message
        
    def process_data(self, lfl_path, data_path, output_path,
                     superframes_in_memory, frame_doubled, plot_changed):
        '''
        :param lfl_path: Path of LFL file.
        :type lfl_path: str
        :param output_path: Output path of HDF file.
        :type output_path: str
        :param superframes_in_memory: Number of superframes to process in memory.
        :type superframes_in_memory: int
        :param frame_doubled: Whether or not the raw data file is frame doubled.
        :type frame_doubled: bool
        :param plot_changed: Whether or not to plot parameters which change within the LFL.
        :type plot_changed: bool        
        '''
        # Load config to read AXIS groups.
        try:
            config = configobj.ConfigObj(lfl_path)
        except configobj.ConfigObjError as err:
            self._queue_error_message('Error while parsing LFL!', str(err))
            raise ValueError(str(err))
        
        if self._last_config:
            for param_name, param_conf in config['Parameters'].iteritems():
                # TODO: Param added, not only changed.
                if param_conf != self._last_config['Parameters'][param_name]:
                    self._changed_params.add(param_name)
        
        self._last_config = dict(config)
        
        axes = {1: ['Altitude STD']}
        if plot_changed and self._changed_params:
            # Add an axis for parameters which have changed.
            axes[2] = list(self._changed_params)
            
        # Read AXIS_* parameter groups.
        axis_offset = len(axes)
        group_index = 1
        while True:
            group_name = 'AXIS_%d' % group_index
            try:
                axes[group_index + axis_offset] = config['Parameter Group'][group_name]
            except KeyError:
                break
            group_index += 1
        
        if len(axes) == 1:
            message = 'AXIS_1 parameter group is not defined! Please define ' \
                      'a parameter group within the LFL named AXIS_1. ' \
                      'Subsequent axes can be defined with groups named ' \
                      'AXIS_2, AXIS_3, etc.'
            self._queue_error_message('AXIS_1 group missing', message)
            raise ValueError(message)
        
        # Create a list of all parameters within the groups.
        param_names = set(itertools.chain.from_iterable(axes.values()))
        
        try:
            lfl_parser, param_list = parse_lfl(lfl_path,
                                               param_names=param_names,
                                               frame_doubled=frame_doubled,
                                               verbose=True)
        except Exception as err:
            show_error_dialog('Error while parsing LFL!', str(err))
            raise ValueError(str(err))
        print 'Processing HDF file.'
        try:
            create_hdf(data_path, output_path, lfl_parser.frame, param_list,
                       superframes_in_memory=superframes_in_memory)
        except Exception as err:
            message = 'Error occurred during processing. Please ensure both ' \
                      'the LFL and raw data file are correct. Exception: %s' \
                      % err
            self._queue_error_message('Processing failed!', message)
            raise ValueError(message)
            
        print 'Finished processing.'
        return axes
    
    def process_loop(self, lfl_path, function):
        '''
        The processing loop.
        '''
        prev_mtime = None
        while True:
            #print 'process loop'
            mtime = os.path.getmtime(lfl_path)
            if not prev_mtime or mtime > prev_mtime:
                if self._ready_to_plot.is_set():
                    self._ready_to_plot.clear()
                try:
                    self._axes = function()
                except ValueError as e:
                    continue
                except ProcessError:
                    self.exit_loop.set()
                    return
                else:
                    self._ready_to_plot.set()
                finally:
                    prev_mtime = mtime
            else:
                time.sleep(1)

    def run(self):
        '''
        The plotting loop.
        '''
        while True:
            # For some strange reason it appears that printing the following
            # line affects the plotting window being shown on windows.
            #print 'plot loop'
            if self.exit_loop.is_set():
                return
            error_message = self._get_error_message()
            if error_message:
                print 'Displaying error message.'
                show_error_dialog(*error_message)
                continue
            if self._ready_to_plot.is_set():
                self._ready_to_plot.clear()
                plot_parameters(self._hdf_path, self._axes)
            else:
                time.sleep(1)


def show_error_dialog(title, message):
    '''
    Show error.
    '''
    # By default an empty Tk main window appears along with message dialogs,
    # the following two lines will hide it.
    window = Tkinter.Tk()
    window.wm_withdraw()        
    tkMessageBox.showerror(title=title, message=message)
    # If we don't explicitly destroy the window, subsequent matplotlib windows
    # will hang.
    window.destroy()


def file_dialogs():
    window = Tkinter.Tk()
    window.wm_withdraw()
    lfl_path = askopenfilename(title='Please choose an LFL file.',
                               filetypes=[("LFL Files","*.lfl")])
    if not lfl_path:
        show_error_dialog('Error!', 'An LFL file must be selected.')
        sys.exit(1)
    data_path = askopenfilename(title='Please choose a raw data file to process.',
                                filetypes=[("All Files","*"),
                                           ("DAT Files","*.dat"),
                                           ("COP Files","*.COP")])
    if not data_path:
        show_error_dialog('Error!', 'A raw data file must be selected.')    
        sys.exit(1)
    window.destroy()
    return lfl_path, data_path


def main():
    # Check if first argument is an option or a path.
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-') \
       and not '-h' in sys.argv:
        parser = create_parser(True)
        args = parser.parse_args()
        lfl_path = args.lfl_path
        data_path = args.data_path        
    else:
        # Input paths from dialog.
        lfl_path, data_path = file_dialogs()
        parser = create_parser(False)
        args = parser.parse_args()        
    
    plot_args = validate_args(lfl_path, data_path, args)        
    
    lfl_path = plot_args[0]
    hdf_path = plot_args[2]
    finished_event = threading.Event()
    process_thread = ProcessAndPlotLoops(hdf_path, args.plot_changed)
    plot_func = lambda: process_thread.process_data(*plot_args)
    process_thread.start()
    try:
        process_thread.process_loop(lfl_path, plot_func)
    except KeyboardInterrupt:
        print 'Setting exit_loop event.'
        process_thread.exit_loop.set()
    finally:
        # If the file is in a temporary location, remove it.
        if not args.output_path:
            if os.path.exists(hdf_path):
                try:
                    os.remove(hdf_path)
                except (OSError, IOError):
                    print 'Could not remove HDF file.'    


if __name__ == '__main__':
    main()