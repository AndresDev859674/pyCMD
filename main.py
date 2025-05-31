import os
import sys
import subprocess
import re
from io import StringIO
import io
import threading # For background command execution
import queue # For inter-thread communication
import traceback # Import for traceback handling
import platform # Added for systeminfo command
import json # Added for session serialization

# Imports for checking and elevating privileges on Windows
import ctypes
# Try importing win32api and win32con, but continue without them if unavailable
try:
    import win32api
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("win32api or win32con not found. Windows-specific admin functions will not be available.")


from PySide6.QtCore import Qt, QTimer, QSize, Signal, QThread, QObject
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QTextEdit, QLineEdit, QTabWidget,
                               QMessageBox, QMenu, QFileDialog, QInputDialog, QDialog, QComboBox,
                               QSplitter, QProgressBar) # QProgressBar added
from PySide6.QtGui import QIcon, QAction, QPalette, QColor, QTextCursor, QFont, QPixmap

# Try importing win32mica but continue without it if unavailable
try:
    from win32mica import ApplyMica
    HAS_MICA = True
except ImportError:
    HAS_MICA = False
    print("win32mica not found. Mica effect will not be applied.")

# Function to check if the application is running as administrator
def is_admin():
    """Checks if the current process has administrator privileges on Windows."""
    if os.name == 'nt' and HAS_WIN32: # Only for Windows and if win32api is available
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except:
            return False
    return False # Not Windows or win32api not available

# Function to restart the application with administrator privileges
def run_as_admin():
    """Starts the application with administrator privileges on Windows."""
    if os.name == 'nt' and HAS_WIN32:
        try:
            # sys.argv[0] is the path to the current script
            # 'runas' is the verb to execute as administrator
            # The second argument is the list of arguments for the new process
            # The third argument is the working directory
            # The fourth argument is the window mode (SW_SHOWNORMAL)
            win32api.ShellExecute(
                0,
                "runas",
                sys.executable, # The Python executable
                " ".join(sys.argv), # All arguments passed to the script
                None, # Working directory
                win32con.SW_SHOWNORMAL # Show the window normally
            )
            return True
        except Exception as e:
            print(f"Error trying to run as administrator: {e}")
            return False
    return False # Not Windows or win32api not available


# Class to handle command execution in a separate thread
class CommandExecutorThread(QThread):
    # Signals to communicate output and prompts to the GUI
    output_received = Signal(str, QColor)
    prompt_detected = Signal(str) # This signal now only indicates a prompt, the GUI will handle it with a dialog
    command_finished = Signal(int)
    error_occurred = Signal(str)

    def __init__(self, command_args, cwd, input_queue, parent=None): # Changed command to command_args (list)
        super().__init__(parent)
        self.command_args = command_args # Store as list of arguments
        self.cwd = cwd
        self.input_queue = input_queue
        self.process = None
        self._is_running = True

    def run(self):
        try:
            # Use subprocess.Popen to execute the command in the background
            # and capture stdin/stdout/stderr for real-time interaction
            # shell=False when command_args is a list
            self.process = subprocess.Popen(
                self.command_args, # Pass command_args directly
                shell=False, # Explicitly set to False
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1, # 1-byte buffer for real-time output
                universal_newlines=True, # Treat stdin/stdout/stderr as text
                cwd=self.cwd,
                encoding='utf-8', # Ensure correct encoding
                errors='replace' # Replace undecodable characters
            )

            # Threads to read stdout and stderr non-blockingly
            stdout_thread = threading.Thread(target=self._read_stream, args=(self.process.stdout, False))
            stderr_thread = threading.Thread(target=self._read_stream, args=(self.process.stderr, True))

            stdout_thread.start()
            stderr_thread.start()

            # Wait for reading threads to finish
            stdout_thread.join()
            stderr_thread.join()

            # Wait for the process to finish and emit the return code
            return_code = self.process.wait()
            self.command_finished.emit(return_code)

        except Exception as e:
            self.error_occurred.emit(f"Error executing command: {e}")
        finally:
            self._is_running = False # Mark the thread as not running

    def _read_stream(self, stream, is_stderr):
        """Reads the stream (stdout or stderr) and emits signals."""
        while self._is_running and self.process.poll() is None:
            try:
                line = stream.readline()
                if not self._is_running: # Check again after readline()
                    break
                if line:
                    # Detect input prompts (more generic to capture any input request)
                    # Look for common prompt patterns: ends with ?, :, or contains (something/something)
                    if re.search(r'[\?\:]\s*$', line) or \
                       re.search(r'\(.*\)\s*:\s*$', line) or \
                       re.search(r'\(S/N\)\s*$', line, re.IGNORECASE) or \
                       re.search(r'\(Y/N\)\s*$', line, re.IGNORECASE) or \
                       re.search(r'Press any key to continue', line, re.IGNORECASE) or \
                       re.search(r'>>>\s*$', line): # Added for Python interactive prompt
                        self.prompt_detected.emit(line.strip()) # Emit the full prompt
                        # Wait for user input from the queue (comes from the GUI dialog)
                        user_input = None
                        while self._is_running and user_input is None: # Loop until input is received or thread stops
                            try:
                                user_input = self.input_queue.get(timeout=0.1) # Short timeout to allow checking _is_running
                            except queue.Empty:
                                QThread.msleep(10) # Small pause to avoid busy-waiting
                                continue
                        
                        if self._is_running and self.process and self.process.stdin: # Only write if still running
                            self.process.stdin.write(user_input + '\n')
                            self.process.stdin.flush()
                        if user_input is not None: # Only mark task done if something was retrieved
                            self.input_queue.task_done()
                    else:
                        color = QColor(255, 0, 0) if is_stderr else QColor(255, 255, 255)
                        self.output_received.emit(line, color)
                else:
                    # If no more lines, the stream might be closed or empty
                    if self.process.poll() is not None: # If the process has ended
                        break
                    QThread.msleep(10) # Small pause to avoid excessive CPU usage
            except Exception as e:
                # Handle potential IOError when stream is closed/terminated
                if not self._is_running: # If we are stopping, this error is expected
                    break
                self.error_occurred.emit(f"Error reading stream: {e}")
                break

    def send_input(self, text):
        """Sends input to the process via the queue."""
        self.input_queue.put(text)

    def stop(self):
        """Stops the thread and the process if it's running."""
        self._is_running = False # Set the flag first
        if self.process and self.process.poll() is None: # Check if the process is still running
            self.process.terminate() # Try to terminate gracefully
            self.process.wait(timeout=5) # Wait a bit for it to terminate
            if self.process.poll() is None: # If still running after terminate
                self.process.kill() # Force termination
        self.wait() # Wait for the QThread to finish its run() method


# New class for a single terminal pane
class TerminalPane(QWidget):
    # Signals to communicate with the parent window
    prompt_detected = Signal(str, object) # prompt_text, pane_instance
    command_finished_in_pane = Signal(int, object) # return_code, pane_instance
    output_received_in_pane = Signal(str, QColor, object) # text, color, pane_instance
    error_occurred_in_pane = Signal(str, object) # error_msg, pane_instance
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont("Consolas", 10))
        self.output_text.setTextColor(QColor(255, 255, 255))
        self.output_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.layout.addWidget(self.output_text)

        # Layout for command entry and progress bar
        command_input_layout = QHBoxLayout()
        command_input_layout.setContentsMargins(0, 0, 0, 0)
        command_input_layout.setSpacing(5) # Add some spacing between elements

        self.command_entry = QLineEdit()
        self.command_entry.setPlaceholderText("Enter command...")
        command_input_layout.addWidget(self.command_entry, 1) # Stretch factor 1

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False) # Hide percentage text
        self.progress_bar.setRange(0, 0) # Indeterminate mode (busy indicator)
        self.progress_bar.setMaximumHeight(20) # Limit height for a sleek look
        self.progress_bar.hide() # Initially hidden
        command_input_layout.addWidget(self.progress_bar)

        self.layout.addLayout(command_input_layout)

        self.command_thread = None
        self.input_queue = queue.Queue()
        self.awaiting_input = False # Flag for this specific pane

        # Command history for this pane
        self.command_history = []
        self.history_index = -1 # -1 means no history item is currently selected

        # Context menu for output area (for copy/paste)
        self.output_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_text.customContextMenuRequested.connect(self.show_output_context_menu)

    def show_output_context_menu(self, pos):
        menu = self.output_text.createStandardContextMenu()
        menu.exec(self.output_text.mapToGlobal(pos))

    def keyPressEvent(self, event):
        """Handles key press events for the command entry, specifically for history navigation."""
        if event.key() == Qt.Key.Key_Up:
            if self.command_history and self.history_index < len(self.command_history) - 1:
                self.history_index += 1
                self.command_entry.setText(self.command_history[len(self.command_history) - 1 - self.history_index])
                event.accept()
            else:
                super().keyPressEvent(event) # Pass to default handler if no history or at end
        elif event.key() == Qt.Key.Key_Down:
            if self.command_history and self.history_index > 0:
                self.history_index -= 1
                self.command_entry.setText(self.command_history[len(self.command_history) - 1 - self.history_index])
                event.accept()
            elif self.command_history and self.history_index == 0:
                self.history_index = -1
                self.command_entry.clear()
                event.accept()
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            # When Enter is pressed, reset history index and add current command to history
            user_input = self.command_entry.text().strip()
            if user_input and not self.awaiting_input: # Only add to history if it's a new command, not input to a prompt
                self.command_history.append(user_input)
                # Keep history size reasonable, e.g., last 100 commands
                if len(self.command_history) > 100:
                    self.command_history.pop(0) # Remove oldest command
            self.history_index = -1 # Reset history index
            super().keyPressEvent(event) # Let QLineEdit handle the returnPressed signal
        else:
            self.history_index = -1 # Reset history index on any other key press (unless it's a modifier key)
            super().keyPressEvent(event)


    def start_command_execution(self, command_args, cwd, interpreter): # Changed command to command_args (list)
        # Stop any existing thread for this pane
        if self.command_thread and self.command_thread.isRunning():
            self.command_thread.stop()
            self.command_thread = None
            self.input_queue = queue.Queue() # Reset queue for new command
            self.awaiting_input = False

        # command_args is now already a list from PyCMDWindow.execute_command_in_pane
        self.command_thread = CommandExecutorThread(
            command_args, cwd, self.input_queue # Pass command_args directly
        )
        self.command_thread.output_received.connect(
            lambda text, color: self.output_received_in_pane.emit(text, color, self)
        )
        self.command_thread.prompt_detected.connect(
            lambda prompt_text: self.prompt_detected.emit(prompt_text, self)
        )
        self.command_thread.command_finished.connect(
            lambda return_code: self.command_finished_in_pane.emit(return_code, self)
        )
        self.command_thread.error_occurred.connect(
            lambda error_msg: self.error_occurred_in_pane.emit(error_msg, self)
        )
        self.command_thread.start()
        self.command_entry.setPlaceholderText("Command running...")
        self.command_entry.setEnabled(False) # Disable input while command is running
        self.progress_bar.show() # Show the progress bar

    def send_input_to_command(self, text):
        """Sends input to the process via the queue."""
        if self.command_thread:
            self.command_thread.send_input(text)

    def append_output(self, text, color):
        cursor = self.output_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.output_text.setTextCursor(cursor)

        # Check for ANSI escape codes
        ansi_escape_pattern = re.compile(r'\x1b\[([0-9;]*)m')
        
        if ansi_escape_pattern.search(text):
            # If ANSI codes are present, convert to HTML
            html_content = self._ansi_to_html(text)
            self.output_text.insertHtml(html_content)
        else:
            # No ANSI codes, apply QColor directly
            self.output_text.setTextColor(color)
            self.output_text.insertPlainText(text)
            self.output_text.setTextColor(QColor(255, 255, 255)) # Restore default after plain text

        self.output_text.ensureCursorVisible()

    def _ansi_to_html(self, ansi_text):
        # Basic ANSI to HTML converter
        html_output = ""
        current_fg_color = "#FFFFFF"  # Default white
        current_bg_color = "#191919"  # Dark background, matching QTextEdit's background

        # ANSI color codes mapping (hex values for common 3/4-bit colors)
        ansi_fg_colors = {
            '30': '#000000', '31': '#FF0000', '32': '#00FF00', '33': '#FFFF00',
            '34': '#0000FF', '35': '#FF00FF', '36': '#00FFFF', '37': '#FFFFFF',
            '90': '#808080', '91': '#FF8080', '92': '#80FF80', '93': '#FFFF80',
            '94': '#8080FF', '95': '#FF80FF', '96': '#80FFFF', '97': '#FFFFFF' # Bright white
        }
        ansi_bg_colors = {
            '40': '#000000', '41': '#800000', '42': '#008000', '43': '#808000',
            '44': '#000080', '45': '#800080', '46': '#008080', '47': '#C0C0C0', # Light grey
            '100': '#808080', '101': '#FF0000', '102': '#00FF00', '103': '#FFFF00',
            '104': '#0000FF', '105': '#FF00FF', '106': '#00FFFF', '107': '#FFFFFF'
        }

        # Regex to find ANSI escape sequences and split the string
        ansi_escape_pattern = re.compile(r'(\x1b\[[0-9;]*m)')
        parts = ansi_escape_pattern.split(ansi_text)

        for part in parts:
            if ansi_escape_pattern.match(part):
                # This is an ANSI escape sequence
                codes_str = part[2:-1] # Remove \x1b[ and m
                codes = codes_str.split(';') if codes_str else ['0'] # Handle empty code (e.g., \x1b[m) as reset

                for code in codes:
                    if code == '0': # Reset all attributes
                        current_fg_color = "#FFFFFF"
                        current_bg_color = "#191919"
                    elif code in ansi_fg_colors:
                        current_fg_color = ansi_fg_colors[code]
                    elif code in ansi_bg_colors:
                        current_bg_color = ansi_bg_colors[code]
                    # Add handling for bold (1), underline (4), etc. if needed
                    # For simplicity, we're focusing on colors here.
            else:
                # This is plain text, apply current colors and escape HTML special characters
                escaped_text = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_output += f"<span style='color:{current_fg_color}; background-color:{current_bg_color};'>{escaped_text}</span>"
        
        return html_output


    def set_awaiting_input(self, state):
        self.awaiting_input = state
        if state:
            self.command_entry.setPlaceholderText("Awaiting input...")
            self.command_entry.setEnabled(True)
            self.command_entry.setFocus()
            self.progress_bar.hide() # Hide progress bar when awaiting input
        else:
            self.command_entry.setPlaceholderText("Enter command...")
            self.command_entry.setEnabled(True) # Re-enable after input is sent or command finishes
            # Progress bar might still be visible if command is running in background

    def stop_pane_thread(self):
        if self.command_thread:
            self.command_thread.stop()
            self.command_thread = None
            self.input_queue = queue.Queue() # Clear queue
            self.awaiting_input = False
            self.command_entry.setPlaceholderText("Enter command...")
            self.command_entry.setEnabled(True)
            self.progress_bar.hide() # Hide progress bar when thread is stopped


class PyCMDWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Initialize admin status at startup
        self.is_admin_mode = is_admin()
        self.base_title = "pyCMD 25.0.1.0"
        self.setWindowTitle(f"{self.base_title} (Administrator)" if self.is_admin_mode else self.base_title)
        self.setWindowIcon(QIcon("icon.png"))  # Correct method with QIcon
        self.setGeometry(100, 100, 850, 450)  # (x_pos, y_pos, width, height)
        
        # First, initialize variables
        self.username = os.getlogin() # Get current username
        self.hostname = platform.node() # Get current hostname
        self.command_history = [] # Global history (though pane history is now used)
        self.current_directory = os.getcwd()
        self.python_environment = {}
        self.welcome_message = r"""_________      _____  ________   
______ ___.__.\_   ___ \   /     \ \______ \  
\____ <   |  |/    \  \/  /  \ /  \ |    |  \ 
|  |_> >___  |\     \____/    Y    \|    `   \
|   __// ____| \______  /\____|__  /_______  /
|__|   \/             \/         \/        \/ 
pyCMD 25.0 [Version 25.0.1.0] (stable build)
Andrew Studios (C) All Rights Reserved

This pyCMD Program Can Cause Damage To The System!
Execute or search for a safe command

Use the 'View' menu to split terminal panes.
""" # Removed leading spaces and initial empty line
        self.current_command_thread = None # Active command thread (global, for dialog handling)
        self.current_input_queue = None # Input queue for the active thread (global)
        self.awaiting_input = False # Global flag to know if any pane is awaiting input
        self.selected_interpreter = "cmd" # Default command interpreter selected

        # Internal pyCMD variables
        self.pycmd_variables = {
            "PATH": os.environ.get('PATH', ''),
            "HOME": os.path.expanduser('~'),
            "USER": self.username,
            "HOSTNAME": self.hostname,
            "pyCMD_pid": str(os.getpid()),
            "pyCMD_history": os.path.join(os.path.expanduser('~'), '.pycmd_history') # Placeholder for history file
        }
        self.last_command_status = 0 # Initialize $status variable

        # Configuration for auto-save/load
        self.config_dir = os.path.join(os.path.expanduser('~'), '.pycmd')
        self.config_file = os.path.join(self.config_dir, 'config.json')
        self.auto_session_file = os.path.join(self.config_dir, 'auto_session.session')
        
        self.auto_save_enabled = False
        self.auto_load_enabled = False

        # Load configuration before setting up UI to reflect saved preferences
        self._load_config()

        # Then set up the UI
        self.setup_ui()
        if HAS_MICA:
            self.apply_mica_effect()
        self.setup_menu()
        
        # Show development warning
        self.show_development_warning()

        # Attempt to auto-load session if enabled and file exists
        self._auto_load_session()
        
    def apply_mica_effect(self):
        """Applies Windows 11 Mica effect to the window background"""
        try:
            hwnd = self.winId()
            ApplyMica(hwnd, True)  # True for dark mode
        except Exception as e:
            print(f"Error applying Mica effect: {e}")
        
    def setup_ui(self):
        """Configures the main user interface"""
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(5)

        # Layout for interpreter selection
        interpreter_layout = QHBoxLayout()
        interpreter_label = QLabel("Interpreter:")
        interpreter_label.setStyleSheet("color: white; font-weight: bold;")
        self.interpreter_combo = QComboBox()
        self.interpreter_combo.addItems(["Windows CMD", "PowerShell", "pyCMD"])
        self.interpreter_combo.setStyleSheet("""
            QComboBox {
                background: rgba(40, 40, 40, 0.9);
                color: white;
                border: 1px solid #555;
                border-radius: 6px;
                padding: 5px;
                min-width: 120px;
            }
            QComboBox::drop-down {
                border: 0px; /* Remove dropdown border */
            }
            QComboBox QAbstractItemView {
                background-color: #303030;
                color: white;
                selection-background-color: #0078D7;
            }
        """)
        self.interpreter_combo.currentIndexChanged.connect(self.set_interpreter)

        interpreter_layout.addWidget(interpreter_label)
        interpreter_layout.addWidget(self.interpreter_combo)
        interpreter_layout.addStretch(1) # Pushes elements to the left

        self.main_layout.addLayout(interpreter_layout) # Add this layout to the main layout
        
        # Configure tabs
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_tab)
        self.tab_widget.setMovable(True) # Allows moving tabs
        
        # Set context menu policy for the tab bar
        self.tab_widget.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tab_widget.tabBar().customContextMenuRequested.connect(self.show_tab_context_menu)

        self.main_layout.addWidget(self.tab_widget)

        # Label for "No tabs open" message
        self.no_tabs_message_label = QLabel(
            "<h2 style='color: white; text-align: center;'>No tabs open.</h2>"
            "<p style='color: white; text-align: center;'>Please go to 'File' menu and select 'New Tab' to start.</p>"
        )
        self.no_tabs_message_label.setAlignment(Qt.AlignCenter)
        self.no_tabs_message_label.setStyleSheet("background-color: #202020; border-radius: 12px; padding: 20px;")
        self.no_tabs_message_label.hide() # Initially hidden

        self.main_layout.addWidget(self.no_tabs_message_label)

        # Create first tab (this will be replaced if auto-load happens)
        self.create_new_tab("System Symbol")
        
        # Configure style for main elements
        self.setStyleSheet("""
            QMainWindow {
                background-color: #202020;
            }
            QTabWidget::pane {
                border: 1px solid #444;
                background: rgba(30, 30, 30, 0.9);
                border-radius: 12px; /* More rounded */
                padding: 5px; /* Space around tab content */
            }
            QTabBar::tab {
                background: rgba(60, 60, 60, 0.7);
                color: white; /* White text color for tabs */
                padding: 8px 16px; /* More padding */
                border-top-left-radius: 12px; /* More rounded */
                border-top-right-radius: 12px; /* More rounded */
                margin-right: 4px; /* More space between tabs */
                border: none;
                min-width: 100px; /* Minimum width for tabs */
                margin-bottom: -1px; /* Overlap with pane border for floating effect */
            }
            QTabBar::tab:selected {
                background: rgba(90, 90, 90, 0.9);
                border-bottom: 3px solid #0078D7; /* Thicker bottom border */
                font-weight: bold; /* Bold text for selected tab */
            }
            QTabBar::tab:hover {
                background: rgba(80, 80, 80, 0.8);
            }
            QTextEdit, QLineEdit {
                background: rgba(25, 25, 25, 0.9);
                color: white;
                border: 1px solid #444;
                border-radius: 8px; /* More rounded */
                padding: 8px; /* More padding */
                font-family: Consolas;
                font-size: 10pt;
                selection-background-color: #0078D7;
            }
            QLineEdit {
                padding: 10px; /* More padding for input */
            }
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #555, stop:1 #333); /* Gradient */
                color: white;
                border: 1px solid #666;
                border-radius: 8px; /* More rounded */
                padding: 8px 16px;
                min-width: 100px;
                font-weight: bold;
                box-shadow: 2px 2px 5px rgba(0,0,0,0.5); /* Shadow */
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #666, stop:1 #444);
                border-color: #0078D7;
            }
            QPushButton:pressed {
                background: #0078D7;
                box-shadow: inset 1px 1px 3px rgba(0,0,0,0.5); /* Press effect */
            }
            QScrollBar:vertical {
                background: rgba(40, 40, 40, 0.7);
                width: 12px;
                margin: 0px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: rgba(100, 100, 100, 0.7);
                min-height: 20px;
                border-radius: 6px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QMenu {
                background-color: #303030; /* Dark background for menus */
                color: white;
                border: 1px solid #555;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background-color: #0078D7; /* Blue highlight */
                color: white;
            }
            QDialog {
                background-color: #252525;
                color: white;
                border-radius: 10px;
            }
            QInputDialog {
                background-color: #252525;
                color: white;
                border-radius: 10px;
            }
            QMessageBox {
                background-color: #252525;
                color: white;
                border-radius: 10px;
            }
            QProgressBar {
                border: 1px solid #444;
                border-radius: 8px;
                background-color: rgba(25, 25, 25, 0.9);
                text-align: center;
                color: white;
            }
            QProgressBar::chunk {
                background-color: #0078D7; /* Blue color for the chunk */
                border-radius: 7px; /* Slightly smaller to fit inside */
                margin: 1px; /* Small margin for chunk separation effect */
            }
        """)
        
    def create_new_tab(self, title="System Symbol", group_name="Default", initial_content="", initial_cwd=None, initial_interpreter=None, pane_data=None):
        """Creates a new tab in the application, with optional initial content, group, and pane structure."""
        # Hide the "no tabs open" message and show the tab widget
        self.no_tabs_message_label.hide()
        self.tab_widget.show()

        if initial_cwd is None:
            initial_cwd = self.current_directory
        if initial_interpreter is None:
            initial_interpreter = self.selected_interpreter

        # Prompt for tab title and group if not provided (for user-initiated new tab)
        if title == "System Symbol" and group_name == "Default" and not initial_content and pane_data is None:
            text, ok = QInputDialog.getText(self, "New Tab", "Enter tab title:", QLineEdit.Normal, title)
            if not ok or not text:
                return None
            title = text

            group, ok = QInputDialog.getText(self, "New Tab Group", "Enter group name (e.g., Development, Production):", QLineEdit.Normal, group_name)
            if not ok or not group:
                group_name = "Default" # Fallback to default if cancelled or empty
            else:
                group_name = group

        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        if pane_data:
            # Reconstruct panes from saved data
            main_splitter = self._create_panes_from_data(pane_data)
            tab_layout.addWidget(main_splitter)
            # Find the first pane to set its group name and initial prompt
            first_pane = self._find_first_terminal_pane(main_splitter)
            if first_pane:
                first_pane.group_name = group_name
                # The content is already loaded from pane_data, just add the prompt
                first_pane.append_output(f"\n{self._get_current_prompt()}", QColor(0, 255, 0))
                first_pane.command_entry.setFocus()
        else:
            # Create a single initial terminal pane if no pane_data
            main_splitter = QSplitter(Qt.Horizontal)
            tab_layout.addWidget(main_splitter)
            initial_pane = self._create_terminal_pane()
            initial_pane.group_name = group_name
            main_splitter.addWidget(initial_pane)
            
            if initial_content:
                initial_pane.output_text.setText(initial_content)
            else:
                initial_pane.output_text.setText(self.welcome_message)
            
            initial_pane.append_output(f"\n{self._get_current_prompt()}", QColor(0, 255, 0))
            initial_pane.command_entry.setFocus()
        
        # Add tab with group name prefix
        full_tab_title = f"[{group_name}] {title}" if group_name != "Default" else title
        tab_index = self.tab_widget.addTab(tab, full_tab_title)
        self.tab_widget.setCurrentIndex(tab_index)
        
        # Auto-save after creating a new tab
        if self.auto_save_enabled:
            self._auto_save_session_silent()

        return tab

    def _create_terminal_pane(self):
        """Helper to create and configure a new TerminalPane."""
        new_pane = TerminalPane(self)
        # Connect the QLineEdit's returnPressed signal directly to PyCMDWindow's handler
        # This allows PyCMDWindow to know which pane sent the command
        new_pane.command_entry.returnPressed.connect(lambda: self.handle_command_input(new_pane))
        
        # Connect signals from the new pane to PyCMDWindow's slots
        new_pane.prompt_detected.connect(self.show_prompt_dialog)
        new_pane.command_finished_in_pane.connect(self.command_thread_finished)
        new_pane.output_received_in_pane.connect(self.append_output)
        new_pane.error_occurred_in_pane.connect(self.append_output_error)

        return new_pane

    def append_output_error(self, error_msg, pane_instance):
        pane_instance.append_output(error_msg, QColor(255, 0, 0))

    def set_interpreter(self, index):
        """Sets the selected command interpreter."""
        if index == 0:
            self.selected_interpreter = "cmd"
        elif index == 1:
            self.selected_interpreter = "powershell"
        elif index == 2: # Handle "pyCMD" selection
            self.selected_interpreter = "pycmd"
        
        current_widget = self.tab_widget.currentWidget()
        # Find the active pane to display the message
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"Interpreter set to: {self.selected_interpreter.upper()}\n", QColor(0, 255, 255)) # Light blue
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Display new prompt
        else:
            # Fallback if no pane is focused (e.g., on initial load)
            self.show_native_message("Interpreter Change", f"Interpreter set to: {self.selected_interpreter.upper()}")
    
    def handle_command_input(self, pane_instance):
        """Handles user input from a specific pane's QLineEdit."""
        command_entry = pane_instance.command_entry
        user_input = command_entry.text().strip()
        
        # If the specific pane is awaiting input, send it to its thread
        if pane_instance.awaiting_input and pane_instance.command_thread:
            pane_instance.append_output(f"<span style='color:#00FF00;'>{user_input}</span>\n", QColor(0, 255, 0)) # Show input in green
            pane_instance.send_input_to_command(user_input)
            pane_instance.set_awaiting_input(False)
            command_entry.setPlaceholderText("Enter command...")
            command_entry.setEnabled(True)
        else:
            # Echo the user's typed command to the output area
            pane_instance.append_output(f"{self._get_current_prompt()}{user_input}\n", QColor(255, 255, 255))
            # If not awaiting input, execute a new command in THIS pane
            self.execute_command_in_pane(pane_instance, user_input)
        
        command_entry.clear()
        # The prompt is now added by command_thread_finished for external commands,
        # or by internal commands themselves.
        pane_instance.output_text.moveCursor(QTextCursor.End)

    def show_tab_context_menu(self, pos):
        """Shows the context menu for tabs (native style)"""
        tab_index = self.tab_widget.tabBar().tabAt(pos)
        if tab_index >= 0:
            menu = QMenu(self)
            # Explicitly set stylesheet for the menu to ensure visibility
            # This ensures the menu uses the dark theme even if global styles are overridden later.
            menu.setStyleSheet("""
                QMenu {
                    background-color: #303030; /* Dark background for menus */
                    color: white;
                    border: 1px solid #555;
                    border-radius: 5px;
                }
                QMenu::item:selected {
                    background-color: #0078D7; /* Blue highlight */
                    color: white;
                }
            """)
            
            rename_action = QAction("Rename Tab", self)
            rename_action.triggered.connect(lambda: self.rename_tab(tab_index))
            menu.addAction(rename_action)
            
            duplicate_action = QAction("Duplicate Tab", self)
            duplicate_action.triggered.connect(lambda: self.duplicate_tab(tab_index))
            menu.addAction(duplicate_action)

            close_action = QAction("Close Tab", self)
            close_action.triggered.connect(lambda: self.close_tab(tab_index))
            menu.addAction(close_action)
            
            menu.exec(self.tab_widget.mapToGlobal(pos))
    
    def close_tab(self, index):
        """Closes a tab. If it's the last tab, displays a message."""
        widget = self.tab_widget.widget(index)
        # Recursively stop all threads in all panes within this tab
        self._stop_all_pane_threads(widget)
        self.tab_widget.removeTab(index)

        if self.tab_widget.count() == 0:
            self.tab_widget.hide()
            self.no_tabs_message_label.show()
        
    def closeEvent(self, event):
        """Overrides the close event to ask for confirmation before exiting."""
        reply = QMessageBox.question(
            self,
            "Exit Confirmation",
            "Are you sure you want to exit pyCMD?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Stop all running command threads before closing
            for i in range(self.tab_widget.count()):
                widget = self.tab_widget.widget(i)
                self._stop_all_pane_threads(widget)
            event.accept()
        else:
            event.ignore()
    
    def _stop_all_pane_threads(self, widget):
        """Recursively stops command threads in all TerminalPanes within a widget."""
        if isinstance(widget, TerminalPane):
            widget.stop_pane_thread()
        elif isinstance(widget, QSplitter):
            for i in range(widget.count()):
                self._stop_all_pane_threads(widget.widget(i))
        elif isinstance(widget, QWidget) and widget.layout():
            for i in range(widget.layout().count()):
                item = widget.layout().itemAt(i)
                if item.widget():
                    self._stop_all_pane_threads(item.widget())

    def rename_tab(self, index):
        """Renames a tab and allows changing its group."""
        current_title = self.tab_widget.tabText(index)
        current_widget = self.tab_widget.widget(index)
        
        # Get the primary pane from the current tab (assuming it's the first widget in the main splitter)
        main_splitter = current_widget.layout().itemAt(0).widget()
        if isinstance(main_splitter, QSplitter):
            current_pane = main_splitter.widget(0)
            current_group = current_pane.group_name
        else: # Fallback if for some reason it's not a splitter (shouldn't happen with current logic)
            current_pane = current_widget.findChild(TerminalPane)
            current_group = "Default" # Fallback

        # Extract original title without group prefix
        original_title_match = re.match(r'\[(.*?)\]\s*(.*)', current_title)
        if original_title_match:
            display_title = original_title_match.group(2)
        else:
            display_title = current_title # No group prefix found

        # Dialog for new title
        new_title, ok = QInputDialog.getText(
            self, "Rename Tab", "Enter new tab title:",
            QLineEdit.Normal, display_title
        )
        if not ok or not new_title:
            return

        # Dialog for new group
        new_group, ok = QInputDialog.getText(
            self, "New Tab Group", "Enter new group name:",
            QLineEdit.Normal, current_group
        )
        if not ok or not new_group:
            new_group = current_group # Keep old group if cancelled or empty

        # Update pane's group name
        if current_pane:
            current_pane.group_name = new_group

        # Update tab title with new group prefix
        full_new_title = f"[{new_group}] {new_title}" if new_group != "Default" else new_title
        self.tab_widget.setTabText(index, full_new_title)

        # Auto-save after renaming a tab
        if self.auto_save_enabled:
            self._auto_save_session_silent()

    def duplicate_tab(self, index):
        """Duplicates the selected tab, preserving its content, colors, and split layout."""
        source_tab_widget = self.tab_widget.widget(index)
        source_tab_title = self.tab_widget.tabText(index)

        # Extract original title and group
        title_match = re.match(r'\[(.*?)\]\s*(.*)', source_tab_title)
        if title_match:
            source_group = title_match.group(1)
            base_title = title_match.group(2)
        else:
            source_group = "Default"
            base_title = source_tab_title

        # Get the structured data of the source tab's layout
        main_splitter = source_tab_widget.layout().itemAt(0).widget()
        if not isinstance(main_splitter, QSplitter):
            self.show_native_message("Duplication Error", "Could not find main splitter in source tab.", QMessageBox.Critical)
            return
        
        pane_data = self._get_pane_data(main_splitter)

        # Create a new tab using the extracted structured data
        new_tab_title = f"Copied - {base_title}"
        self.create_new_tab(
            title=new_tab_title,
            group_name=source_group,
            pane_data=pane_data # Pass the structured pane data
        )
        # Auto-save after duplicating a tab (already handled by create_new_tab, but explicitly for clarity)
        # if self.auto_save_enabled:
        #     self._auto_save_session_silent()


    def show_native_message(self, title, message, icon=QMessageBox.Information):
        """Shows a message with native style"""
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(message)
        msg.setIcon(icon)
        msg.setStyleSheet("")  # Reset to native style
        msg.exec()
    
    def show_development_warning(self):
        """Shows the development warning"""
        self.show_native_message("Warning", "This is a Development Project! It may contain instability", QMessageBox.Warning)
    
    def show_changelog(self):
        """Shows the changelog with native style and better formatting"""
        changelog = """
        <b>pyCMD 25.0.1.0 Changelog:</b>
        <ul>
            <li><b>Improved Session Management:</b>
                <ul>
                    <li>**Auto Session & Auto Load Session:** New functionality to automatically save and load the last session.</li>
                    <li>**Save Session:** Significant improvement in the ability to save the current session's configuration.</li>
                </ul>
            </li>
            <li><b>User Interface & Usability Enhancements:</b>
                <ul>
                    <li>**Improved Tab Duplication:** The process of duplicating tabs is now more efficient and robust.</li>
                    <li>**"No Tabs Open" Message:** A clear message is displayed when all tabs are closed, guiding the user to open a new one.</li>
                    <li>**Set pyCMD as Default:** Option to set pyCMD as the default program for certain file types.</li>
                    <li>**ProgressBar in Command Execution:** A progress bar is now displayed when running commands, providing visual feedback.</li>
                    <li>**Enhanced Help Window:** More detailed and useful information available in the help window.</li>
                </ul>
            </li>
            <li><b>Enhanced Compatibility & Emulation:</b>
                <ul>
                    <li>**ANSI Compatibility:** Improved support for ANSI escape sequences for richer display.</li>
                    <li>**Major pyCMD Interpreter Improvement:** Significant advancements in the capability and stability of the internal pyCMD interpreter.</li>
                    <li>**Improved Emulation & Colors:** More authentic terminal emulation experience with better color support.</li>
                    <li>**Improved Python Traceback Display:** Internal Python errors now show full tracebacks in the terminal output.</li>
                    <li>**Direct File Opening:** pyCMD.exe can now directly open files with `.rcmd`, `.sh`, `.bat`, `.sessions`, and `.vbs` extensions.</li>
                    <li>**Enhanced Command Emulation:** The current working directory is now displayed as a prompt before each command for a more authentic terminal experience.</li>
                </ul>
            </li>
            <li><b>Performance & Optimization:</b>
                <ul>
                    <li>**General Optimization:** Improvements in application performance and efficiency.</li>
                </ul>
            </li>
        </ul>
        """
        self.show_native_message("pyCMD Changelog", changelog, QMessageBox.Information)

    def show_about(self):
        """Shows 'About' information with native style and better formatting"""
        msg = QMessageBox(self)
        msg.setWindowTitle("About pyCMD")
        msg.setIconPixmap(QPixmap("icon.png").scaled(64, 64, Qt.KeepAspectRatio))  # Adds an icon
        
        about_text = """
        <div style='text-align: center;'>
            <h2>pyCMD</h2>
            <p><i>Advanced Command Line Interface</i></p>
            <hr>
            <p>Created by <b>Andrew Studios</b></p>
            <p>Version 25.0.1.0 (Stable Build)</p>
            <p>© 2025 All Rights Reserved</p>
            <hr>
            <p style='font-size: small;'>
                This program is protected by copyright law and international treaties.
            </p>
        </div>
        """
        
        msg.setTextFormat(Qt.TextFormat.RichText)  # Allows HTML formatting
        msg.setText(about_text)
        
        msg.setStyleSheet("")  # Native Windows style
        msg.setStandardButtons(QMessageBox.Ok)
        msg.setDefaultButton(QMessageBox.Ok)
        msg.setMinimumSize(400, 300)
        
        msg.exec()
    
    def _get_current_prompt(self):
        """Generates the dynamic prompt string."""
        return f"{self.username}@{self.hostname}:{self.current_directory}> "

    def execute_command_in_pane(self, pane_instance, command):
        """Executes a command within a specific TerminalPane."""
        output_text = pane_instance.output_text

        if not command:
            # If command is empty, just add a new prompt
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return
        
        # History is now managed by TerminalPane.keyPressEvent

        # Stop any previous command thread for THIS pane
        if pane_instance.command_thread and pane_instance.command_thread.isRunning():
            pane_instance.stop_pane_thread()

        # Flag to check if an internal pyCMD command was handled
        command_handled_internally = False

        try: # Wrap internal command handling in a try-except for traceback
            # Custom pyCMD commands (these are always handled internally)
            if command.lower().startswith("pycmd echocolor="):
                self.handle_echocolor(command, pane_instance) # Pass pane_instance
                command_handled_internally = True
            elif command.lower() == "cls":
                output_text.clear()
                output_text.setText(self.welcome_message)
                pane_instance.append_output(f"\n{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt immediately
                command_handled_internally = True
            elif command.lower() == "help":
                self.show_help()
                command_handled_internally = True
            elif command.lower().startswith("cd "):
                self.change_directory(command, pane_instance) # Pass pane_instance
                command_handled_internally = True
            elif command.lower() == "pycmd save":
                self.save_session()
                command_handled_internally = True
            elif command.lower() == "pycmd open":
                self.open_session()
                command_handled_internally = True
            elif command.lower() == "pycmd create rcmd":
                self.create_rcmd_command()
                command_handled_internally = True
            elif command.lower() == "pycmd modify rcmd": # Handle internal command
                self.modify_rcmd_command()
                command_handled_internally = True
            elif command.lower() == "pycmd rcmd":
                self.execute_rcmd_file(pane_instance) # Pass pane_instance here
                command_handled_internally = True
            elif command.lower() == "pycmd admin_only_command": # Example of an admin-only command
                if self.is_admin_mode:
                    pane_instance.append_output("<span style='color: yellow;'>[ADMIN MODE] Executing sensitive operation...</span>\n", QColor(255, 255, 0))
                    # Admin command logic would go here
                else:
                    pane_instance.append_output("<span style='color: red;'>Access Denied: This command requires Administrator privileges.</span>\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower() == "pycmd systeminfo": # New systeminfo command
                self._handle_systeminfo(pane_instance)
                command_handled_internally = True
            elif command.lower() == "ls": # New ls command
                self._handle_ls(pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("set "): # New set command
                self._handle_set_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("echo "): # Enhanced echo command
                self._handle_echo_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower() == "pwd": # New pwd command
                self._handle_pwd_command(pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("open "): # New open command
                self._handle_open_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("math "): # New math command
                self._handle_math_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("read "): # New read command
                self._handle_read_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("type "): # New type command
                self._handle_type_command(command, pane_instance)
                command_handled_internally = True
            # Handle "python" as an external command if not in "pycmd" interpreter mode
            elif command.lower().startswith("python ") or command.lower() == "python":
                if self.selected_interpreter == "pycmd":
                    # If in pyCMD interpreter mode, treat "python" as an internal Python code execution
                    # This is for executing Python *snippets* directly within pyCMD's context
                    self.execute_python_code(command, pane_instance) # Pass pane_instance directly
                    command_handled_internally = True
                else:
                    # If in CMD or PowerShell mode, treat "python" as an external executable
                    # This will run the system's python.exe
                    if self.selected_interpreter == "cmd":
                        command_args = ["cmd.exe", "/c", command]
                    elif self.selected_interpreter == "powershell":
                        command_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
                    else:
                        command_args = [command] # Fallback
                    pane_instance.start_command_execution(command_args, self.current_directory, self.selected_interpreter)
                    command_handled_internally = True
            elif command.lower().startswith("pycmd autosave "):
                state = command[len("pycmd autosave "):].strip().lower()
                if state == "on":
                    self.toggle_auto_save(True)
                    pane_instance.append_output("Auto Save Session: ON\n", QColor(0, 255, 0))
                elif state == "off":
                    self.toggle_auto_save(False)
                    pane_instance.append_output("Auto Save Session: OFF\n", QColor(255, 255, 0))
                else:
                    pane_instance.append_output("Error: Invalid argument for pycmd autosave. Use 'on' or 'off'.\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower().startswith("pycmd autoload "):
                state = command[len("pycmd autoload "):].strip().lower()
                if state == "on":
                    self.toggle_auto_load(True)
                    pane_instance.append_output("Auto Load Session: ON\n", QColor(0, 255, 0))
                elif state == "off":
                    self.toggle_auto_load(False)
                    pane_instance.append_output("Auto Load Session: OFF\n", QColor(255, 255, 0))
                else:
                    pane_instance.append_output("Error: Invalid argument for pycmd autoload. Use 'on' or 'off'.\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower() == "pycmd autosave_now":
                self._auto_save_session_silent()
                pane_instance.append_output("Session auto-saved silently.\n", QColor(0, 255, 0))
                command_handled_internally = True
            
            # If the command was not handled by an internal pyCMD command (and not a python command)
            if not command_handled_internally:
                if self.selected_interpreter == "pycmd":
                    pane_instance.append_output(f"Error: Unrecognized pyCMD internal command: '{command}'\n", QColor(255, 0, 0))
                    pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
                else:
                    # Execute via subprocess for CMD or PowerShell commands
                    if self.selected_interpreter == "cmd":
                        command_args = ["cmd.exe", "/c", command]
                    elif self.selected_interpreter == "powershell":
                        command_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
                    else:
                        command_args = [command] # Fallback, though should be covered by interpreter check

                    pane_instance.start_command_execution(command_args, self.current_directory, self.selected_interpreter)
                    # Prompt will be added by command_thread_finished for external commands

        except Exception:
            # Catch any Python errors in internal commands and print traceback
            pane_instance.append_output(f"An internal pyCMD error occurred:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

        output_text.moveCursor(QTextCursor.End)
    
    def append_output(self, text, color, pane_instance): # Now takes pane_instance
        """Appends text to a specific pane's QTextEdit with the specified color."""
        pane_instance.append_output(text, color)

    def show_prompt_dialog(self, prompt_text, pane_instance): # Now takes pane_instance
        """Shows a dialog for the user to enter a response to a prompt for a specific pane."""
        self.awaiting_input = True # Global flag is still useful for overall state
        pane_instance.set_awaiting_input(True) # Set awaiting input for this specific pane
        pane_instance.command_entry.setEnabled(False) # Disable its input
        
        # Show the prompt in the pane's main text area
        pane_instance.append_output(f"<span style='color: yellow;'>{prompt_text}</span>\n", QColor(255, 255, 0))
        
        dialog_title = "Command Input Required"
        if ">>>" in prompt_text: # Simple heuristic for Python interactive prompt
            dialog_title = "Python Interactive Input"
            prompt_text += "\n(Type 'exit()' or 'quit()' to leave Python interactive mode)"

        user_input, ok = QInputDialog.getText(
            self, dialog_title, prompt_text, QLineEdit.Normal, ""
        )
        
        if ok and user_input is not None:
            # If the user entered something and pressed OK
            pane_instance.append_output(f"<span style='color:#00FF00;'>{user_input}</span>\n", QColor(0, 255, 0)) # Show user input in console
            pane_instance.send_input_to_command(user_input)
        else:
            # If the user cancelled or entered nothing, send a newline or empty string
            # This might cause the command to fail or cancel depending on how the process handles it.
            pane_instance.append_output(f"<span style='color:red;'>[Input Cancelled/Empty]</span>\n", QColor(255, 0, 0))
            pane_instance.send_input_to_command("") # Send empty to not block the process
            
        pane_instance.set_awaiting_input(False)
        pane_instance.command_entry.setEnabled(True)
        pane_instance.command_entry.setFocus()
        pane_instance.output_text.moveCursor(QTextCursor.End)
    
    def command_thread_finished(self, return_code, pane_instance): # Now takes pane_instance
        """Called when a command thread finishes for a specific pane."""
        self.last_command_status = return_code # Update $status
        pane_instance.append_output(f"\nCommand finished with exit code {return_code}\n", QColor(100, 100, 255))
        pane_instance.stop_pane_thread() # Clean up thread for this pane
        pane_instance.command_entry.setPlaceholderText("Enter command...")
        pane_instance.command_entry.setEnabled(True)
        pane_instance.command_entry.setFocus()
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add new prompt
        pane_instance.output_text.moveCursor(QTextCursor.End)

        # Trigger auto-save if enabled
        if self.auto_save_enabled:
            self._auto_save_session_silent()

    def handle_echocolor(self, command, pane_instance): # Changed to take pane_instance
        """Handles the custom echocolor command"""
        try:
            # Extract color and text from the command using regex
            match = re.match(r"pycmd echocolor=\((\w+)\)=\(\"(.*?)\"\)", command, re.IGNORECASE)
            if match:
                color_name = match.group(1).lower()  # Color to apply
                text = match.group(2)  # Text to display
                
                # Color mapping
                color_map = {
                    'red': QColor(255, 0, 0),
                    'green': QColor(0, 255, 0),
                    'yellow': QColor(255, 255, 0),
                    'blue': QColor(0, 0, 255),
                    'magenta': QColor(255, 0, 255),
                    'cyan': QColor(0, 255, 255),
                    'white': QColor(255, 255, 255),
                    'grey': QColor(128, 128, 128),
                    'light_red': QColor(255, 128, 128),
                    'light_green': QColor(128, 255, 128),
                    'light_yellow': QColor(255, 255, 128),
                    'light_blue': QColor(128, 128, 255),
                    'light_magenta': QColor(255, 128, 255),
                    'light_cyan': QColor(128, 255, 255),
                    'light_white': QColor(255, 255, 255),
                    'light_grey': QColor(192, 192, 192)
                }
                
                if color_name in color_map:
                    pane_instance.append_output(text + "\n", color_map[color_name]) # Use pane_instance.append_output
                else:
                    pane_instance.append_output(f"Error: '{color_name}' is not a valid color.\n", QColor(255, 0, 0)) # Use pane_instance.append_output
                    pane_instance.append_output(f"Valid colors: {', '.join(color_map.keys())}\n", QColor(255, 255, 255)) # Use pane_instance.append_output
            else:
                pane_instance.append_output("Error: Invalid command format. Use pyCMD echocolor=(*color*)=(\"*text*\")\n", QColor(255, 0, 0)) # Use pane_instance.append_output
            self.last_command_status = 0 # Assuming echocolor itself doesn't fail unless format is wrong
        except Exception:
            # Catch any Python errors in internal commands and print traceback
            pane_instance.append_output(f"An internal pyCMD error occurred in echocolor:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def change_directory(self, command, pane_instance): # Changed to take pane_instance
        """Changes the current directory"""
        new_directory = command[3:].strip()
        try:
            os.chdir(new_directory)
            self.current_directory = os.getcwd()
            pane_instance.append_output(f"Directory changed to {self.current_directory}\n", QColor(0, 255, 0)) # Use pane_instance.append_output
            self.last_command_status = 0
        except Exception:
            pane_instance.append_output(f"Error changing directory:\n{traceback.format_exc()}\n", QColor(255, 0, 0)) # Use pane_instance.append_output
            self.show_native_message("Error", f"Error changing directory: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
        
    def execute_python_code(self, command, pane_instance): # Changed to take pane_instance
        """Executes Python code entered by the user (for pyCMD interpreter mode)"""
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        new_stdout = StringIO()
        new_stderr = StringIO()
        sys.stdout = new_stdout
        sys.stderr = new_stderr

        try:
            # Handle 'python ' prefix for Python commands
            # If command is just "python", treat as empty code (no output)
            code_to_execute = command[7:].strip() if command.lower().startswith("python ") else ""
            
            if code_to_execute:
                exec(code_to_execute, self.python_environment)
            
            output = new_stdout.getvalue()
            error = new_stderr.getvalue()
            
            if output:
                pane_instance.append_output(output, QColor(255, 255, 255)) # Use pane_instance.append_output
            if error:
                pane_instance.append_output("Error: " + error, QColor(255, 0, 0)) # Use pane_instance.append_output
            self.last_command_status = 0
        except Exception:
            pane_instance.append_output(f"Error executing Python code:\n{traceback.format_exc()}\n", QColor(255, 0, 0)) # Use pane_instance.append_output
            self.show_native_message("Python Execution Error", f"Error executing Python code: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
    
    def _handle_systeminfo(self, pane_instance):
        """Displays system information."""
        info = []
        info.append("--- System Information ---")
        info.append(f"Operating System: {platform.system()} {platform.release()} ({platform.version()})")
        info.append(f"Architecture: {platform.machine()}")
        info.append(f"Processor: {platform.processor()}")
        info.append(f"Python Version (pyCMD internal): {platform.python_version()}")
        info.append(f"Current Directory: {self.current_directory}")
        info.append(f"User: {self.username}")
        info.append(f"Hostname: {self.hostname}")
        info.append("--------------------------")
        pane_instance.append_output("\n".join(info) + "\n", QColor(128, 255, 255)) # Light Cyan
        self.last_command_status = 0
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _handle_ls(self, pane_instance):
        """Lists directory contents (for pyCMD interpreter mode)."""
        try:
            items = os.listdir(self.current_directory)
            
            # Sort items: directories first, then files, both alphabetically
            items.sort(key=lambda x: (not os.path.isdir(os.path.join(self.current_directory, x)), x.lower()))

            html_output = ""
            
            for item in items:
                full_path = os.path.join(self.current_directory, item)
                display_name = item
                color_hex = "#FFFFFF" # Default white for files

                if os.path.isdir(full_path):
                    display_name += "/" # Append slash for directories
                    color_hex = "#80FFFF" # Light cyan for directories
                elif os.path.isfile(full_path) and os.access(full_path, os.X_OK):
                    color_hex = "#00FF00" # Green for executable files (basic check)

                html_output += f"<span style='color:{color_hex};'>{display_name}</span><br>"
            
            pane_instance.output_text.insertHtml(html_output)
            self.last_command_status = 0

        except Exception:
            pane_instance.append_output(f"Error listing directory: {traceback.format_exc()}\n", QColor(255, 0, 0))
            self.show_native_message("Error", f"Error listing directory: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _handle_set_command(self, command, pane_instance):
        """Handles the 'set' command for internal pyCMD variables."""
        parts = command.split(' ', 1) # Split into 'set' and the rest
        if len(parts) == 1: # Just 'set' - list all variables
            pane_instance.append_output("--- pyCMD Variables ---\n", QColor(255, 255, 0))
            # Include standard variables and custom ones
            all_vars = {**self.pycmd_variables, "$status": str(self.last_command_status)}
            for key, value in all_vars.items():
                pane_instance.append_output(f"{key}={value}\n", QColor(255, 255, 255))
            pane_instance.append_output("-----------------------\n", QColor(255, 255, 0))
            self.last_command_status = 0
        else:
            arg = parts[1].strip()
            if '=' in arg: # set <var_name>=<value>
                var_name, value = arg.split('=', 1)
                self.pycmd_variables[var_name.upper()] = value # Store in uppercase for consistency
                pane_instance.append_output(f"Variable '{var_name.upper()}' set to '{value}'\n", QColor(0, 255, 0))
                self.last_command_status = 0
            else: # set <var_name> - display value
                var_name = arg.upper()
                if var_name == "STATUS": # Special case for $status
                    pane_instance.append_output(f"STATUS={self.last_command_status}\n", QColor(255, 255, 255))
                    self.last_command_status = 0
                elif var_name in self.pycmd_variables:
                    pane_instance.append_output(f"{var_name}={self.pycmd_variables[var_name]}\n", QColor(255, 255, 255))
                    self.last_command_status = 0
                else:
                    pane_instance.append_output(f"Error: Variable '{var_name}' not found.\n", QColor(255, 0, 0))
                    self.last_command_status = 1
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _handle_echo_command(self, command, pane_instance):
        """Handles the 'echo' command, expanding variables."""
        text_to_echo = command[len("echo"):].strip()
        
        # Simple variable expansion: find $VAR_NAME and replace
        # This regex looks for $ followed by alphanumeric characters or underscore
        def replace_var(match):
            var_name = match.group(1).upper() # Convert to uppercase for lookup
            if var_name == "STATUS":
                return str(self.last_command_status)
            return self.pycmd_variables.get(var_name, f"${match.group(1)}") # Return original if not found

        expanded_text = re.sub(r'\$([a-zA-Z0-9_]+)', replace_var, text_to_echo)
        
        pane_instance.append_output(expanded_text + "\n", QColor(255, 255, 255))
        self.last_command_status = 0
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _handle_pwd_command(self, pane_instance):
        """Handles the 'pwd' command."""
        pane_instance.append_output(self.current_directory + "\n", QColor(255, 255, 255))
        self.last_command_status = 0
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _handle_open_command(self, command, pane_instance):
        """Handles the 'open' command to open a file with its default application."""
        file_path = command[len("open"):].strip()
        if not file_path:
            pane_instance.append_output("Error: 'open' command requires a file path.\n", QColor(255, 0, 0))
            self.last_command_status = 1
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return

        full_path = os.path.join(self.current_directory, file_path)
        
        if not os.path.exists(full_path):
            pane_instance.append_output(f"Error: File not found: '{full_path}'\n", QColor(255, 0, 0))
            self.last_command_status = 1
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return

        try:
            if platform.system() == "Windows":
                os.startfile(full_path)
            elif platform.system() == "Darwin": # macOS
                subprocess.run(['open', full_path], check=True)
            else: # Linux and other Unix-like
                subprocess.run(['xdg-open', full_path], check=True)
            pane_instance.append_output(f"Opened '{full_path}' with default application.\n", QColor(0, 255, 0))
            self.last_command_status = 0
        except Exception as e:
            pane_instance.append_output(f"Error opening file '{full_path}': {e}\n", QColor(255, 0, 0))
            self.show_native_message("Error Opening File", f"Error opening file: {e}", QMessageBox.Critical)
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))

    def _handle_math_command(self, command, pane_instance):
        """Handles the 'math' command for basic arithmetic evaluation."""
        expression = command[len("math"):].strip()
        if not expression:
            pane_instance.append_output("Error: 'math' command requires an expression.\n", QColor(255, 0, 0))
            self.last_command_status = 1
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return

        try:
            # Basic security for eval: limit globals and builtins
            # Only allow a safe subset of operations
            safe_dict = {
                '__builtins__': {
                    'abs': abs, 'max': max, 'min': min, 'round': round,
                    'sum': sum, 'len': len, 'int': int, 'float': float,
                    'str': str, 'bool': bool
                },
                'True': True, 'False': False, 'None': None
            }
            result = eval(expression, {"__builtins__": safe_dict['__builtins__']}, safe_dict)
            pane_instance.append_output(f"{result}\n", QColor(255, 255, 255))
            self.last_command_status = 0
        except Exception as e:
            pane_instance.append_output(f"Error evaluating math expression: {e}\n", QColor(255, 0, 0))
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))

    def _handle_read_command(self, command, pane_instance):
        """Handles the 'read' command to read input into a variable."""
        parts = command.split(' ', 1)
        if len(parts) < 2:
            pane_instance.append_output("Error: 'read' command requires a variable name.\nUsage: read <variable_name>\n", QColor(255, 0, 0))
            self.last_command_status = 1
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return
        
        var_name = parts[1].strip().upper() # Convert to uppercase for internal storage

        user_input, ok = QInputDialog.getText(
            self, "Read Input", f"Enter value for '{var_name}':", QLineEdit.Normal, ""
        )
        
        if ok:
            self.pycmd_variables[var_name] = user_input
            pane_instance.append_output(f"Variable '{var_name}' set to '{user_input}'\n", QColor(0, 255, 0))
            self.last_command_status = 0
        else:
            pane_instance.append_output(f"Read operation cancelled.\n", QColor(255, 255, 0))
            self.last_command_status = 1
            
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))

    def _handle_type_command(self, command, pane_instance):
        """Handles the 'type' command to indicate how a command would be interpreted."""
        cmd_to_type = command[len("type"):].strip()
        if not cmd_to_type:
            pane_instance.append_output("Error: 'type' command requires a command name.\n", QColor(255, 0, 0))
            self.last_command_status = 1
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))
            return

        internal_commands = [
            "cls", "help", "ls", "pycmd", "cd", "set", "echo", "pwd",
            "open", "math", "read", "type", "python" # 'python' is internal in pyCMD mode
        ]
        
        if cmd_to_type.lower() in internal_commands:
            pane_instance.append_output(f"{cmd_to_type} is a pyCMD internal command.\n", QColor(0, 255, 0))
            self.last_command_status = 0
        elif self.selected_interpreter != "pycmd":
            # Check if it's a system executable in CMD/PowerShell mode
            try:
                # Use 'where' on Windows, 'which' on Linux/macOS
                check_cmd = "where" if platform.system() == "Windows" else "which"
                result = subprocess.run([check_cmd, cmd_to_type], capture_output=True, text=True, check=False, shell=False)
                if result.returncode == 0:
                    pane_instance.append_output(f"{cmd_to_type} is a system executable: {result.stdout.strip()}\n", QColor(0, 255, 0))
                    self.last_command_status = 0
                else:
                    pane_instance.append_output(f"{cmd_to_type} is not found as an internal or system command.\n", QColor(255, 255, 0))
                    self.last_command_status = 1
            except FileNotFoundError:
                pane_instance.append_output(f"Error: '{check_cmd}' command not found. Cannot determine type of '{cmd_to_type}'.\n", QColor(255, 0, 0))
                self.last_command_status = 1
            except Exception as e:
                pane_instance.append_output(f"An error occurred while checking command type: {e}\n", QColor(255, 0, 0))
                self.last_command_status = 1
        else: # pyCMD mode, and not an internal command
            pane_instance.append_output(f"{cmd_to_type} is not found as a pyCMD internal command.\n", QColor(255, 255, 0))
            self.last_command_status = 1
        
        pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0))


    def create_rcmd_command(self):
        """Creates an RCMD file with commands"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Create RCMD Command")
        dialog.resize(700, 500)
        dialog.setStyleSheet("")  # Native style
        
        layout = QVBoxLayout(dialog)
        
        text_edit = QTextEdit()
        save_button = QPushButton("Save Commands")
        save_button.clicked.connect(lambda: self.save_rcmd_file(text_edit, dialog))
        
        layout.addWidget(text_edit)
        layout.addWidget(save_button)
        
        dialog.exec()
    
    def save_rcmd_file(self, text_edit, dialog):
        """Saves commands to an RCMD file"""
        try: # Add try-except for traceback
            commands = text_edit.toPlainText().strip().split('\n')
            if commands:
                file_path, _ = QFileDialog.getSaveFileName(
                    self, "Save RCMD File", "", "Command Files (*.rcmd)"
                )
                if file_path:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        for cmd in commands:
                            if cmd.strip():  # Ignore empty lines
                                f.write(cmd.strip() + "\n")
                    
                    current_widget = self.tab_widget.currentWidget()
                    # Find the active pane to display the message
                    focused_pane = self._get_focused_terminal_pane(current_widget)
                    if focused_pane:
                        focused_pane.append_output(f"Commands saved to {file_path}\n", QColor(0, 255, 0))
                        focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
                    
                    self.show_native_message("RCMD File Saved", f"RCMD file saved to {file_path}.")
                    dialog.close()
            self.last_command_status = 0
        except Exception:
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"Error saving RCMD file:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            self.show_native_message("Error Saving RCMD File", f"Error saving RCMD file: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1

    def modify_rcmd_command(self):
        """Allows the user to modify an existing RCMD file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Modify RCMD File", self.current_directory, "Command Files (*.rcmd);;All Files (*)"
        )
        if not file_path:
            self.last_command_status = 1
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception: # Add try-except for traceback
            self.show_native_message("Error Reading File", f"Could not read RCMD file: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Modify RCMD: {os.path.basename(file_path)}")
        dialog.resize(700, 500)
        dialog.setStyleSheet("") # Native style

        layout = QVBoxLayout(dialog)
        text_edit = QTextEdit()
        text_edit.setPlainText(content)
        
        save_button = QPushButton("Save Changes")
        save_button.clicked.connect(lambda: self._save_modified_rcmd_file(file_path, text_edit.toPlainText(), dialog))

        layout.addWidget(text_edit)
        layout.addWidget(save_button)
        
        dialog.exec()

    def _save_modified_rcmd_file(self, file_path, content, dialog):
        """Saves the modified content back to the RCMD file."""
        try: # Add try-except for traceback
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"RCMD file '{os.path.basename(file_path)}' modified successfully.\n", QColor(0, 255, 0))
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            
            self.show_native_message("RCMD Modified", f"RCMD file '{os.path.basename(file_path)}' saved.")
            dialog.close()
            self.last_command_status = 0
        except Exception:
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"Error saving changes to RCMD file:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            self.show_native_message("Error Saving Changes", f"Could not save changes to RCMD file: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1

    def _execute_rcmd_file_from_path(self, file_path, pane_instance):
        """Executes commands from a given RCMD file path in the specified pane."""
        if not os.path.exists(file_path):
            pane_instance.append_output(f"Error: RCMD file not found: {file_path}\n", QColor(255, 0, 0))
            self.show_native_message("Error", f"RCMD file not found: {file_path}", QMessageBox.Critical)
            self.last_command_status = 1
            return

        pane_instance.append_output(f"Executing commands from {file_path}\n", QColor(100, 100, 255))
        
        try: # Add try-except for traceback
            with open(file_path, 'r', encoding='utf-8') as f:
                for cmd in f:
                    cmd = cmd.strip()
                    if cmd:
                        # Echo the command from the RCMD file
                        pane_instance.append_output(f"{self._get_current_prompt()}{cmd}\n", QColor(255, 255, 255))
                        self._execute_single_command_in_pane(pane_instance, cmd)
            self.last_command_status = 0
        except Exception:
            pane_instance.append_output(f"Error reading or executing RCMD file:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
            self.show_native_message("Error", f"Error reading or executing RCMD file: {traceback.format_exc()}", QMessageBox.Critical)
            self.last_command_status = 1
        finally:
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            # Auto-save after RCMD execution
            if self.auto_save_enabled:
                self._auto_save_session_silent()


    def _execute_single_command_in_pane(self, pane_instance, command):
        """Helper to execute a single command directly within a pane's context."""
        # This logic is similar to a part of execute_command_in_pane, but without
        # the initial "> command" output and command_entry clearing, as it's for internal use.

        # Stop any previous command thread for THIS pane
        if pane_instance.command_thread and pane_instance.command_thread.isRunning():
            pane_instance.stop_pane_thread()

        command_handled_internally = False

        try: # Wrap internal command handling in a try-except for traceback
            if command.lower().startswith("pycmd echocolor="):
                self.handle_echocolor(command, pane_instance)
                command_handled_internally = True
            elif command.lower() == "cls":
                pane_instance.output_text.clear()
                pane_instance.output_text.setText(self.welcome_message)
                pane_instance.append_output(f"\n{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
                command_handled_internally = True
            elif command.lower() == "help":
                self.show_help()
                command_handled_internally = True
            elif command.lower().startswith("cd "):
                self.change_directory(command, pane_instance)
                command_handled_internally = True
            elif command.lower() == "pycmd save":
                self.save_session()
                command_handled_internally = True
            elif command.lower() == "pycmd open":
                self.open_session()
                command_handled_internally = True
            elif command.lower() == "pycmd create rcmd":
                self.create_rcmd_command()
                command_handled_internally = True
            elif command.lower() == "pycmd modify rcmd":
                self.modify_rcmd_command()
                command_handled_internally = True
            elif command.lower() == "pycmd rcmd":
                # Avoid infinite recursion if an RCMD file calls 'pycmd rcmd' without a path
                pane_instance.append_output("Error: 'pycmd rcmd' cannot be called recursively without a specific file path within an RCMD file.\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower() == "pycmd admin_only_command":
                if self.is_admin_mode:
                    pane_instance.append_output("<span style='color: yellow;'>[ADMIN MODE] Executing sensitive operation...</span>\n", QColor(255, 255, 0))
                else:
                    pane_instance.append_output("<span style='color: red;'>Access Denied: This command requires Administrator privileges.</span>\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower() == "pycmd systeminfo": # New systeminfo command
                self._handle_systeminfo(pane_instance)
                command_handled_internally = True
            elif command.lower() == "ls": # New ls command
                self._handle_ls(pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("set "): # New set command
                self._handle_set_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("echo "): # Enhanced echo command
                self._handle_echo_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower() == "pwd": # New pwd command
                self._handle_pwd_command(pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("open "): # New open command
                self._handle_open_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("math "): # New math command
                self._handle_math_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("read "): # New read command
                self._handle_read_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("type "): # New type command
                self._handle_type_command(command, pane_instance)
                command_handled_internally = True
            elif command.lower().startswith("python ") or command.lower() == "python":
                if self.selected_interpreter == "pycmd":
                    self.execute_python_code(command, pane_instance) # Pass pane_instance directly
                    command_handled_internally = True
                else:
                    if self.selected_interpreter == "cmd":
                        command_args = ["cmd.exe", "/c", command]
                    elif self.selected_interpreter == "powershell":
                        command_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
                    else:
                        command_args = [command]
                    pane_instance.start_command_execution(command_args, self.current_directory, self.selected_interpreter)
                    command_handled_internally = True
            elif command.lower().startswith("pycmd autosave "):
                state = command[len("pycmd autosave "):].strip().lower()
                if state == "on":
                    self.toggle_auto_save(True)
                    pane_instance.append_output("Auto Save Session: ON\n", QColor(0, 255, 0))
                elif state == "off":
                    self.toggle_auto_save(False)
                    pane_instance.append_output("Auto Save Session: OFF\n", QColor(255, 255, 0))
                else:
                    pane_instance.append_output("Error: Invalid argument for pycmd autosave. Use 'on' or 'off'.\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower().startswith("pycmd autoload "):
                state = command[len("pycmd autoload "):].strip().lower()
                if state == "on":
                    self.toggle_auto_load(True)
                    pane_instance.append_output("Auto Load Session: ON\n", QColor(0, 255, 0))
                elif state == "off":
                    self.toggle_auto_load(False)
                    pane_instance.append_output("Auto Load Session: OFF\n", QColor(255, 255, 0))
                else:
                    pane_instance.append_output("Error: Invalid argument for pycmd autoload. Use 'on' or 'off'.\n", QColor(255, 0, 0))
                command_handled_internally = True
            elif command.lower() == "pycmd autosave_now":
                self._auto_save_session_silent()
                pane_instance.append_output("Session auto-saved silently.\n", QColor(0, 255, 0))
                command_handled_internally = True

            if not command_handled_internally:
                if self.selected_interpreter == "pycmd":
                    pane_instance.append_output(f"Error: Unrecognized pyCMD internal command: '{command}'\n", QColor(255, 0, 0))
                    pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
                else:
                    if self.selected_interpreter == "cmd":
                        command_args = ["cmd.exe", "/c", command]
                    elif self.selected_interpreter == "powershell":
                        command_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
                    else:
                        command_args = [command]
                    pane_instance.start_command_execution(command_args, self.current_directory, self.selected_interpreter)
                    # Prompt will be added by command_thread_finished for external commands

        except Exception:
            pane_instance.append_output(f"An internal pyCMD error occurred:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
            pane_instance.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt


    def execute_rcmd_file(self, pane_instance=None): # Now accepts optional pane_instance
        """Executes commands from an RCMD file chosen via a file dialog."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open RCMD File", "", "Command Files (*.rcmd)"
        )
        if file_path:
            # If no pane_instance is provided (e.g., from menu), get the currently focused one
            if pane_instance is None:
                current_tab_widget = self.tab_widget.currentWidget()
                pane_instance = self._get_focused_terminal_pane(current_tab_widget)

            if pane_instance:
                self._execute_rcmd_file_from_path(file_path, pane_instance)
            else:
                self.show_native_message("Error", "No active terminal pane found to execute RCMD file.", QMessageBox.Critical)
    
    def save_session(self):
        """Saves the current session to a file, including tab structure, content (with colors), and history."""
        try:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Session", "", "Session Files (*.session)"
            )
            if file_path:
                session_data = []
                for i in range(self.tab_widget.count()):
                    tab_widget = self.tab_widget.widget(i)
                    tab_title = self.tab_widget.tabText(i)
                    
                    # Extract group name from tab title
                    title_match = re.match(r'\[(.*?)\]\s*(.*)', tab_title)
                    group_name = title_match.group(1) if title_match else "Default"
                    base_title = title_match.group(2) if title_match else tab_title

                    # Get the main splitter or pane of the tab
                    main_content_widget = tab_widget.layout().itemAt(0).widget()
                    
                    panes_data = self._get_pane_data(main_content_widget)
                    
                    session_data.append({
                        "title": base_title,
                        "group_name": group_name,
                        "panes_data": panes_data
                    })
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(session_data, f, indent=4)
                
                self.show_native_message("Session Saved", f"Session saved to {file_path}.")
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
        except Exception:
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"Error saving session:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
                focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
            self.show_native_message("Error Saving Session", f"Error saving session: {traceback.format_exc()}", QMessageBox.Critical)

    def _auto_save_session_silent(self):
        """Silently saves the current session to the predefined auto-session file."""
        try:
            os.makedirs(self.config_dir, exist_ok=True) # Ensure config directory exists
            session_data = []
            for i in range(self.tab_widget.count()):
                tab_widget = self.tab_widget.widget(i)
                tab_title = self.tab_widget.tabText(i)
                
                title_match = re.match(r'\[(.*?)\]\s*(.*)', tab_title)
                group_name = title_match.group(1) if title_match else "Default"
                base_title = title_match.group(2) if title_match else tab_title

                main_content_widget = tab_widget.layout().itemAt(0).widget()
                panes_data = self._get_pane_data(main_content_widget)
                
                session_data.append({
                    "title": base_title,
                    "group_name": group_name,
                    "panes_data": panes_data
                })
            
            with open(self.auto_session_file, 'w', encoding='utf-8') as f:
                json.dump(session_data, f, indent=4)
            # print(f"Session auto-saved to {self.auto_session_file}") # For debugging
        except Exception as e:
            print(f"Error during silent auto-save: {e}") # Log error, but don't interrupt user

    def _get_pane_data(self, widget):
        """Recursively extracts data from TerminalPanes and QSplitters."""
        if isinstance(widget, TerminalPane):
            return {
                "type": "pane",
                "content": widget.output_text.toHtml(), # Save as HTML to preserve colors
                "history": widget.command_history
            }
        elif isinstance(widget, QSplitter):
            children_data = []
            for i in range(widget.count()):
                children_data.append(self._get_pane_data(widget.widget(i)))
            return {
                "type": "splitter",
                "orientation": "horizontal" if widget.orientation() == Qt.Horizontal else "vertical",
                "sizes": widget.sizes(), # Save splitter sizes
                "children": children_data
            }
        return None # Should not happen with current UI structure

    def open_session(self, file_path=None): # Modified to accept optional file_path
        """Opens a saved session from a file, restoring tab structure, content, and history."""
        if file_path is None: # If no path is provided, open a file dialog
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Open Session", "", "Session Files (*.session)"
            )
        
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    session_data = json.load(f)
                
                # Clear all existing tabs
                while self.tab_widget.count() > 0:
                    self.close_tab(0) # Use close_tab to ensure threads are stopped
                
                for tab_data in session_data:
                    title = tab_data.get("title", "Restored Tab")
                    group_name = tab_data.get("group_name", "Default")
                    panes_data = tab_data.get("panes_data")

                    # Create a new tab and reconstruct its content
                    self.create_new_tab(
                        title=title,
                        group_name=group_name,
                        pane_data=panes_data # Pass structured data for reconstruction
                    )
                
                self.show_native_message("Session Loaded", f"Session loaded from {file_path}.")
            except Exception:
                current_widget = self.tab_widget.currentWidget()
                focused_pane = self._get_focused_terminal_pane(current_widget)
                if focused_pane:
                    focused_pane.append_output(f"Error loading session:\n{traceback.format_exc()}\n", QColor(255, 0, 0))
                    focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
                self.show_native_message("Error Loading Session", f"Error loading session: {traceback.format_exc()}", QMessageBox.Critical)
        current_widget = self.tab_widget.currentWidget()
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def _auto_load_session(self):
        """Attempts to load the auto-saved session if enabled."""
        if self.auto_load_enabled and os.path.exists(self.auto_session_file):
            print(f"Attempting to auto-load session from {self.auto_session_file}") # For debugging
            self.open_session(self.auto_session_file)
        else:
            print("Auto-load session not enabled or file not found.") # For debugging

    def _create_panes_from_data(self, data):
        """Recursively creates TerminalPanes and QSplitters from structured data."""
        if data["type"] == "pane":
            pane = self._create_terminal_pane()
            pane.output_text.setHtml(data.get("content", "")) # Set HTML content
            pane.command_history = data.get("history", []) # Restore history
            return pane
        elif data["type"] == "splitter":
            splitter = QSplitter(Qt.Horizontal if data.get("orientation") == "horizontal" else Qt.Vertical)
            for child_data in data.get("children", []):
                splitter.addWidget(self._create_panes_from_data(child_data))
            if "sizes" in data and len(data["sizes"]) == splitter.count(): # Only set sizes if count matches
                splitter.setSizes(data["sizes"]) # Restore splitter sizes
            return splitter
        return None

    def _find_first_terminal_pane(self, widget):
        """Recursively finds the first TerminalPane within a widget hierarchy."""
        if isinstance(widget, TerminalPane):
            return widget
        elif isinstance(widget, QSplitter):
            for i in range(widget.count()):
                found_pane = self._find_first_terminal_pane(widget.widget(i))
                if found_pane:
                    return found_pane
        elif isinstance(widget, QWidget) and widget.layout():
            for i in range(widget.layout().count()):
                item = widget.layout().itemAt(i)
                if item.widget():
                    found_pane = self._find_first_terminal_pane(item.widget())
                    if found_pane:
                        return found_pane
        return None
    
    def _load_config(self):
        """Loads auto-save/load configuration from file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                self.auto_save_enabled = config.get('auto_save_enabled', False)
                self.auto_load_enabled = config.get('auto_load_enabled', False)
            except Exception as e:
                print(f"Error loading config file: {e}")
                # Reset to default if loading fails
                self.auto_save_enabled = False
                self.auto_load_enabled = False
        else:
            print("Config file not found. Using default settings.")

    def _save_config(self):
        """Saves auto-save/load configuration to file."""
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            config = {
                'auto_save_enabled': self.auto_save_enabled,
                'auto_load_enabled': self.auto_load_enabled
            }
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            print(f"Error saving config file: {e}")

    def toggle_auto_save(self, checked):
        """Toggles auto-save feature and saves configuration."""
        self.auto_save_enabled = checked
        self._save_config()
        # Update menu action state
        for action in self.findChildren(QAction):
            if action.text() == "Auto Save Session":
                action.setChecked(checked)
                break

    def toggle_auto_load(self, checked):
        """Toggles auto-load feature and saves configuration."""
        self.auto_load_enabled = checked
        self._save_config()
        # Update menu action state
        for action in self.findChildren(QAction):
            if action.text() == "Auto Load Session":
                action.setChecked(checked)
                break
    
    def reload_app(self):
        """Reloads the application"""
        python = sys.executable
        os.execl(python, python, *sys.argv)
        
    def show_help(self):
        """Shows the help for available commands"""
        help_message = """
Available Commands (pyCMD interpreter):
cls - Clear screen
help - Show this help message
ls - List directory contents
set [VAR_NAME[=VALUE]] - Set or display shell variables
echo [TEXT | $VAR_NAME] - Display text or variable values
pwd - Print current working directory
open <file_path> - Open a file with its default application
math <expression> - Perform mathematical calculations
read <variable_name> - Read a line of input into a variable
type <command_name> - Indicate how a command would be interpreted
pyCMD save - Save current session
pyCMD open - Open a saved session
pyCMD create rcmd - Create RCMD commands
pyCMD modify rcmd - Modify an existing RCMD command
pyCMD rcmd - Execute RCMD commands (via file dialog)
pyCMD echocolor=(*color*)=("*text*") - Colored text output
pyCMD admin_only_command - Example of an admin-only command (requires running as Administrator)
pyCMD systeminfo - Display system information
pyCMD autosave [on|off] - Toggle auto-save session
pyCMD autoload [on|off] - Toggle auto-load session
pyCMD autosave_now - Force a silent auto-save

Variables (pyCMD interpreter):
$PATH - System's executable search path
$HOME - User's home directory
$USER - Current username
$HOSTNAME - Current machine hostname
$status - Exit code of the last executed command
$pyCMD_pid - Process ID of the current pyCMD instance
$pyCMD_history - Path to the hypothetical history file

Other Interpreters (Windows CMD, PowerShell):
Most native Windows CMD/PowerShell commands are supported.
"""
        self.show_native_message("Help", help_message)
        current_widget = self.tab_widget.currentWidget()
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt
    
    def show_color_tutorial(self):
        """Shows the tutorial for changing colors"""
        tutorial_message = (
            "How to Create Messages, but with Color! :\n\n"
            "The echocolor command allows you to display colored text.\n"
            "Usage: pyCMD echocolor=(color)=(\"text\")\n\n"
            "Available colors:\n"
            "- red, green, yellow, blue, magenta, cyan, white, grey\n"
            "- light_red, light_green, light_yellow, light_blue\n"
            "- light_magenta, light_cyan, light_white, light_grey\n\n"
            "Example: pyCMD echocolor=(light_blue)=(\"Hello World!\")"
        )
        self.show_native_message("Color Tutorial", tutorial_message)
        current_widget = self.tab_widget.currentWidget()
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def set_pyCMD_default(self):
        """
        Attempts to directly open Windows settings for file associations.
        The user will still need to manually select .rcmd and then pyCMD.
        Includes an explanation why it's not fully automatic.
        """
        if os.name == 'nt':  # Only for Windows
            try:
                # This command opens the Windows 'Default apps by file type' settings page.
                # The user will need to scroll and find .rcmd.
                subprocess.run(["start", "ms-settings:defaultapps-byfiletype"], shell=True, check=True)

                # Message to the user after attempting to open settings
                instructions = """
                <b>Redirected to Windows Settings!</b>
                <p>We've redirected you to the "Default apps by file type" section in Windows Settings.</p>
                <p>Please follow these steps to set pyCMD as the default application for <code>.rcmd</code> files:</p>
                <ol>
                    <li>Scroll down until you find the <code><b>.rcmd</b></code> and <code><b>.sessions</b></code> file extension.</li>
                    <li>Click on the program currently associated with it (or the button to choose one).</li>
                    <li>Select <b>pyCMD</b> from the list. If it's not listed, click "Look for another app on this PC" and navigate to your <code>pyCMD.exe</code> location.</li>
                </ol>
                ---
                <p><b>Why isn't this fully automatic?</b></p>
                <p>This process requires manual steps from you due to fundamental **operating system security restrictions**. Windows (and other modern operating systems) prevents any application from automatically changing file associations without explicit user confirmation.</p>
                <p>This is a crucial security measure to:</p>
                <ul>
                    <li><b>Prevent Malware:</b> Stop malicious software from "hijacking" your file types and running harmful code every time you open a common file like a document or an image.</li>
                    <li><b>Maintain User Control:</b> Ensure that *you*, the user, always have the final say over how your files are opened.</li>
                </ul>
                <p>Even major web browsers follow a similar protocol; they can *request* to be default, but Windows always steps in to require your confirmation. Your pyCMD application, like any other third-party app, must adhere to these protective measures.</p>
                <p>Thank you for your understanding and cooperation!</p>
                """
                self.show_native_message("Set .rcmd Default", instructions, QMessageBox.Information)

            except Exception as e:
                self.show_native_message(
                    "Error Opening Settings",
                    f"Could not open Windows settings. Error: {e}\n\n"
                    "Please navigate manually to: Settings > Apps > Default apps > Choose default apps by file type.",
                    QMessageBox.Critical
                )
        else:
            self.show_native_message(
                "Windows-Only Feature",
                "This automatic redirection feature is only available on Windows.",
                QMessageBox.Information
            )
        current_widget = self.tab_widget.currentWidget()
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt

    def setup_menu(self):
        """Configures the menu bar with native style"""
        menubar = self.menuBar()
        menubar.setStyleSheet("")  # Native style for the menu bar
        
        # pyCMD Menu
        pycmd_menu = menubar.addMenu("pyCMD")
        pycmd_menu.setStyleSheet("")  # Native style
        pycmd_menu.addAction(QIcon.fromTheme("view-refresh"), "Changelog", self.show_changelog)
        pycmd_menu.addAction(QIcon.fromTheme("help-about"), "About", self.show_about)
        pycmd_menu.addSeparator()
        # Removed generic icon, as no specific icon was provided for "preferences-system"
        pycmd_menu.addAction("Set pyCMD default", self.set_pyCMD_default) 
        pycmd_menu.addSeparator()
        pycmd_menu.addAction(QIcon.fromTheme("system-restart"), "Reload Application", self.reload_app)
        pycmd_menu.addAction(QIcon.fromTheme("application-exit"), "Exit", self.close)
        
        # File Menu
        file_menu = menubar.addMenu("File")
        file_menu.setStyleSheet("")  # Native style
        file_menu.addAction(QIcon.fromTheme("document-new"), "New Tab", lambda: self.create_new_tab("System Symbol"))
        
        # Auto Save Session Action
        self.auto_save_action = QAction("Auto Save Session", self, checkable=True)
        self.auto_save_action.setChecked(self.auto_save_enabled)
        self.auto_save_action.triggered.connect(self.toggle_auto_save)
        file_menu.addAction(self.auto_save_action)

        # Auto Load Session Action
        self.auto_load_action = QAction("Auto Load Session", self, checkable=True)
        self.auto_load_action.setChecked(self.auto_load_enabled)
        self.auto_load_action.triggered.connect(self.toggle_auto_load)
        file_menu.addAction(self.auto_load_action)

        file_menu.addSeparator()
        file_menu.addAction(QIcon.fromTheme("document-save"), "Save Session", self.save_session)
        file_menu.addSeparator()
        file_menu.addAction(QIcon.fromTheme("document-open"), "Open Session", self.open_session)
        file_menu.addAction(QIcon.fromTheme("document-properties"), "Create RCMD", self.create_rcmd_command)
        file_menu.addAction(QIcon.fromTheme("document-edit"), "Modify RCMD", self.modify_rcmd_command) # NEW ACTION
        file_menu.addAction(QIcon.fromTheme("system-run"), "Execute RCMD", self.execute_rcmd_file)

        # Edit Menu
        edit_menu = menubar.addMenu("Edit")
        edit_menu.setStyleSheet("")  # Native style
        edit_menu.addAction(QIcon.fromTheme("help-contents"), "Help", self.show_help)
        
        # Admin Menu
        admin_menu = menubar.addMenu("Administrator")
        admin_menu.setStyleSheet("")
        
        # Action to run as administrator
        run_as_admin_action = QAction(QIcon.fromTheme("security-high"), "Run as Administrator", self)
        run_as_admin_action.triggered.connect(self._handle_run_as_admin)
        admin_menu.addAction(run_as_admin_action)
        
        # Disable the action if we are already administrator
        if self.is_admin_mode:
            run_as_admin_action.setEnabled(False)
            run_as_admin_action.setText("Already Running as Administrator")


        # View Menu (New)
        view_menu = menubar.addMenu("View")
        view_menu.setStyleSheet("")
        view_menu.addAction(QIcon.fromTheme("view-split-top-bottom"), "Split Horizontal", self.split_horizontal)
        view_menu.addAction(QIcon.fromTheme("view-split-left-right"), "Split Vertical", self.split_vertical)

        # Customization Menu
        custom_menu = menubar.addMenu("Tutorials")
        custom_menu.setStyleSheet("")  # Native style
        custom_menu.addAction("How to Create Messages, but with Color!", self.show_color_tutorial)

    def _handle_run_as_admin(self):
        """Handles the 'Run as Administrator' action."""
        if not self.is_admin_mode:
            if run_as_admin():
                # If ShellExecute was successful, the current application will close and a new one will start.
                # We don't need to do anything else here.
                sys.exit(0) # Exit the current application
            else:
                self.show_native_message("Elevation Error", "Could not start the application with administrator privileges.", QMessageBox.Critical)
        else:
            self.show_native_message("Information", "The application is already running with administrator privileges.", QMessageBox.Information)
        current_widget = self.tab_widget.currentWidget()
        focused_pane = self._get_focused_terminal_pane(current_widget)
        if focused_pane:
            focused_pane.append_output(f"{self._get_current_prompt()}", QColor(0, 255, 0)) # Add prompt


    def _get_focused_terminal_pane(self, parent_widget):
        """
        Finds the TerminalPane within the given parent_widget that has focus,
        or the first TerminalPane found if none has specific focus.
        """
        if not parent_widget:
            return None

        # List to hold all TerminalPanes found in the current tab's hierarchy
        all_panes_in_tab = []

        # Recursive helper to find all TerminalPanes
        def find_all_terminal_panes(widget):
            if isinstance(widget, TerminalPane):
                all_panes_in_tab.append(widget)
            elif isinstance(widget, QSplitter):
                for i in range(widget.count()):
                    find_all_terminal_panes(widget.widget(i))
            elif isinstance(widget, QWidget) and widget.layout():
                for i in range(widget.layout().count()):
                    item = widget.layout().itemAt(i)
                    if item.widget():
                        find_all_terminal_panes(item.widget())

        find_all_terminal_panes(parent_widget)

        if not all_panes_in_tab:
            return None # No terminal panes found in the current tab

        # Check if any of the found panes' input fields have focus
        focused_widget = QApplication.focusWidget()
        for pane in all_panes_in_tab:
            if focused_widget == pane.command_entry or focused_widget == pane.output_text:
                return pane # Return the pane whose input/output field is focused

        # If no specific input field is focused, return the first pane found
        return all_panes_in_tab[0]

    def split_current_pane(self, orientation):
        current_tab = self.tab_widget.currentWidget()
        if not current_tab:
            return

        focused_pane = self._get_focused_terminal_pane(current_tab)
        if not focused_pane:
            self.show_native_message("Split Error", "Please focus a terminal input field to split it.", QMessageBox.Warning)
            return

        # Find the QSplitter containing the focused_pane
        parent_splitter = focused_pane.parent()
        while parent_splitter and not isinstance(parent_splitter, QSplitter):
            parent_splitter = parent_splitter.parent()

        if not parent_splitter:
            # This ideally shouldn't happen if the initial pane is always in a splitter
            # But as a fallback, if the pane is directly in the tab's layout (no splitter yet),
            # replace it with a new splitter containing the old and new pane.
            tab_layout = current_tab.layout()
            if tab_layout.indexOf(focused_pane) != -1:
                tab_layout.removeWidget(focused_pane)
                focused_pane.setParent(None)
                
                new_splitter = QSplitter(orientation)
                new_splitter.addWidget(focused_pane)
                new_pane = self._create_terminal_pane()
                new_splitter.addWidget(new_pane)
                tab_layout.addWidget(new_splitter)
                focused_pane.command_entry.setFocus()
                # Auto-save after splitting a pane
                if self.auto_save_enabled:
                    self._auto_save_session_silent()
                return
            else:
                self.show_native_message("Split Error", "Could not find a suitable parent splitter for the active pane.", QMessageBox.Warning)
                return

        # If already in a splitter
        if parent_splitter.orientation() == orientation:
            # If the splitter already has the desired orientation, just add a new pane
            new_pane = self._create_terminal_pane()
            parent_splitter.addWidget(new_pane)
            new_pane.command_entry.setFocus()
        else:
            # If the splitter has the opposite orientation, create a nested splitter
            index_in_parent = parent_splitter.indexOf(focused_pane)
            
            # Remove focused_pane from its parent_splitter by setting its parent to None
            focused_pane.setParent(None) 

            # Create a new splitter with the desired orientation
            nested_splitter = QSplitter(orientation)
            nested_splitter.addWidget(focused_pane)
            new_pane = self._create_terminal_pane()
            nested_splitter.addWidget(new_pane)

            # Insert the new nested splitter back into the parent_splitter
            parent_splitter.insertWidget(index_in_parent, nested_splitter)
            focused_pane.command_entry.setFocus() # Keep focus on the original pane

        # Ensure the layout updates
        current_tab.layout().update()
        
        # Auto-save after splitting a pane
        if self.auto_save_enabled:
            self._auto_save_session_silent()


    def split_horizontal(self):
        # This function name now corresponds to the visual effect of a horizontal dividing line (top/bottom panes)
        self.split_current_pane(Qt.Horizontal)

    def split_vertical(self):
        # This function name now corresponds to the visual effect of a vertical dividing line (left/right panes)
        self.split_current_pane(Qt.Vertical)

    def _handle_dragged_file_execution(self, file_path, pane_instance):
        """Handles the execution of dragged and dropped files based on their extension."""
        if not os.path.exists(file_path):
            pane_instance.append_output(f"Error: File not found: {file_path}\n", QColor(255, 0, 0))
            self.last_command_status = 1
            return

        file_extension = os.path.splitext(file_path)[1].lower()
        command_to_execute = None
        interpreter_mode = None

        if file_extension == ".rcmd":
            pane_instance.append_output(f"Executing RCMD file: {file_path}\n", QColor(150, 255, 150))
            self._execute_rcmd_file_from_path(file_path, pane_instance)
            return # RCMD files are handled internally, no external process needed here
        elif file_extension == ".session": # Handle .session files
            pane_instance.append_output(f"Loading session file: {file_path}\n", QColor(150, 255, 150))
            self.open_session(file_path) # Call open_session with the file path
            return # Session files are handled internally, no external process needed here
        elif file_extension == ".bat":
            command_to_execute = ["cmd.exe", "/c", file_path]
            interpreter_mode = "cmd"
        elif file_extension == ".vbs":
            command_to_execute = ["cscript.exe", "//NoLogo", file_path]
            interpreter_mode = "cmd" # cscript is a cmd utility
        elif file_extension == ".sh":
            if platform.system() == "Windows":
                # Try to use bash.exe on Windows (e.g., from Git Bash or WSL)
                try:
                    subprocess.run(["bash.exe", "--version"], check=True, capture_output=True)
                    command_to_execute = ["bash.exe", file_path]
                    interpreter_mode = "powershell" # Can be executed from PowerShell
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pane_instance.append_output(f"Error: .sh files require 'bash.exe' (e.g., Git Bash) to be in your system PATH on Windows.\n", QColor(255, 0, 0))
                    self.last_command_status = 1
                    return
            else: # Linux/macOS
                command_to_execute = ["sh", file_path] # Use default shell
                interpreter_mode = "powershell" # Generic shell interpreter
        else:
            pane_instance.append_output(f"Error: Unsupported file type for direct execution: '{file_extension}'.\n", QColor(255, 0, 0))
            pane_instance.append_output(f"Consider using 'open {file_path}' or switching to 'Windows CMD' or 'PowerShell' interpreter and running the command directly.\n", QColor(255, 255, 0))
            self.last_command_status = 1
            return

        if command_to_execute:
            pane_instance.append_output(f"Executing '{file_path}'...\n", QColor(150, 255, 150))
            pane_instance.start_command_execution(command_to_execute, os.path.dirname(file_path), interpreter_mode)
            # The prompt will be added by command_thread_finished

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Configure Fusion style for a more modern look
    app.setStyle("Fusion")
    
    # Configure dark color palette for the application
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.Window, QColor(45, 45, 45))
    dark_palette.setColor(QPalette.WindowText, Qt.white)
    dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
    dark_palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
    dark_palette.setColor(QPalette.ToolTipText, Qt.white)
    dark_palette.setColor(QPalette.Text, Qt.white)
    dark_palette.setColor(QPalette.Button, QColor(45, 45, 45))
    dark_palette.setColor(QPalette.ButtonText, Qt.white)
    dark_palette.setColor(QPalette.BrightText, Qt.red)
    dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(dark_palette)
    
    window = PyCMDWindow()
    window.show()

    # Handle files opened via command line arguments (e.g., drag and drop)
    if len(sys.argv) > 1:
        file_to_execute = sys.argv[1]
        # Get the initial pane widgets from the first tab
        # Assuming the first tab always has at least one TerminalPane inside its splitter
        first_tab_widget = window.tab_widget.widget(0)
        main_splitter = first_tab_widget.layout().itemAt(0).widget() # Get the splitter
        initial_pane = window._find_first_terminal_pane(main_splitter) # Use helper to find the first pane

        if initial_pane:
            window._handle_dragged_file_execution(file_to_execute, initial_pane)
            # Add prompt after handling command line arguments
            initial_pane.append_output(f"{window._get_current_prompt()}", QColor(0, 255, 0))
        else:
            print("Error: No terminal pane found in the initial tab to handle dragged file.")


    sys.exit(app.exec())
