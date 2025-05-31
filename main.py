import os
import sys
import subprocess
import re
from io import StringIO
import io
import threading # For background command execution
import queue # For inter-thread communication

# Import for checking and elevating privileges on Windows
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
                               QSplitter) # QSplitter added
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
    """Restarts the application with administrator privileges on Windows."""
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

    def __init__(self, command, cwd, input_queue, parent=None):
        super().__init__(parent)
        self.command = command
        self.cwd = cwd
        self.input_queue = input_queue
        self.process = None
        self._is_running = True

    def run(self):
        try:
            # Use subprocess.Popen to execute the command in the background
            # and capture stdin/stdout/stderr for real-time interaction
            self.process = subprocess.Popen(
                self.command,
                shell=True,
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
                if line:
                    # Detect input prompts (more generic to capture any input request)
                    # Look for common prompt patterns: ends with ?, :, or contains (something/something)
                    if re.search(r'[\?\:]\s*$', line) or \
                       re.search(r'\(.*\)\s*:\s*$', line) or \
                       re.search(r'\(S/N\)\s*$', line, re.IGNORECASE) or \
                       re.search(r'\(Y/N\)\s*$', line, re.IGNORECASE) or \
                       re.search(r'Press any key to continue', line, re.IGNORECASE): # For the 'pause' command
                        self.prompt_detected.emit(line.strip()) # Emit the full prompt
                        # Wait for user input from the queue (comes from the GUI dialog)
                        user_input = self.input_queue.get(timeout=60) # Wait up to 60 seconds
                        if self.process and self.process.stdin:
                            self.process.stdin.write(user_input + '\n')
                            self.process.stdin.flush()
                        self.input_queue.task_done() # Mark task as complete
                    else:
                        color = QColor(255, 0, 0) if is_stderr else QColor(255, 255, 255)
                        self.output_received.emit(line, color)
                else:
                    # If no more lines, the stream might be closed or empty
                    if self.process.poll() is not None: # If the process has ended
                        break
                    QThread.msleep(10) # Small pause to avoid excessive CPU usage
            except Exception as e:
                self.error_occurred.emit(f"Error reading stream: {e}")
                break

    def send_input(self, text):
        """Sends input to the process via the queue."""
        self.input_queue.put(text)

    def stop(self):
        """Stops the thread and the process if it's running."""
        self._is_running = False
        if self.process and self.process.poll() is None:
            self.process.terminate() # Try to terminate the process gracefully
            self.process.wait(timeout=5) # Wait a bit
            if self.process.poll() is None:
                self.process.kill() # If it doesn't terminate, kill it
        self.wait() # Wait for the thread to finish


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

        self.command_entry = QLineEdit()
        self.command_entry.setPlaceholderText("Enter command...")
        self.layout.addWidget(self.command_entry)

        self.command_thread = None
        self.input_queue = queue.Queue()
        self.awaiting_input = False # Flag for this specific pane

        # Context menu for output area (for copy/paste)
        self.output_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output_text.customContextMenuRequested.connect(self.show_output_context_menu)

    def show_output_context_menu(self, pos):
        menu = self.output_text.createStandardContextMenu()
        menu.exec(self.output_text.mapToGlobal(pos))

    def start_command_execution(self, command, cwd, interpreter):
        # Stop any existing thread for this pane
        if self.command_thread and self.command_thread.isRunning():
            self.command_thread.stop()
            self.command_thread = None
            self.input_queue = queue.Queue() # Reset queue for new command
            self.awaiting_input = False

        actual_command_to_execute = command
        if interpreter == "powershell":
            escaped_command = command.replace('"', '`"')
            actual_command_to_execute = f"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \"{escaped_command}\""
        elif interpreter == "cmd":
            # subprocess.Popen with shell=True handles cmd.exe /c by default
            pass
        # Note: "pycmd" interpreter commands are handled by PyCMDWindow directly, not by subprocess

        self.command_thread = CommandExecutorThread(
            actual_command_to_execute, cwd, self.input_queue
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

    def send_input_to_command(self, text):
        if self.command_thread:
            self.command_thread.send_input(text)

    def append_output(self, text, color):
        cursor = self.output_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.output_text.setTextCursor(cursor)
        
        self.output_text.setTextColor(color)
        self.output_text.insertPlainText(text)
        self.output_text.setTextColor(QColor(255, 255, 255)) # Restore default color
        self.output_text.ensureCursorVisible() # Ensure the end is visible

    def set_awaiting_input(self, state):
        self.awaiting_input = state
        if state:
            self.command_entry.setPlaceholderText("Awaiting input...")
            self.command_entry.setEnabled(True)
            self.command_entry.setFocus()
        else:
            self.command_entry.setPlaceholderText("Enter command...")
            self.command_entry.setEnabled(True) # Re-enable after input is sent or command finishes

    def stop_pane_thread(self):
        if self.command_thread:
            self.command_thread.stop()
            self.command_thread = None
            self.input_queue = queue.Queue() # Clear queue
            self.awaiting_input = False
            self.command_entry.setPlaceholderText("Enter command...")
            self.command_entry.setEnabled(True)


class PyCMDWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Initialize admin status at startup
        self.is_admin_mode = is_admin()
        self.base_title = "pyCMD 25.0.0.0"
        self.setWindowTitle(f"{self.base_title} (Administrator)" if self.is_admin_mode else self.base_title)
        self.setWindowIcon(QIcon("icon.png"))  # Correct method with QIcon
        self.setGeometry(100, 100, 850, 450)  # (x_pos, y_pos, width, height)
        
        # First, initialize variables
        self.command_history = []
        self.current_directory = os.getcwd()
        self.python_environment = {}
        self.welcome_message = (
            "pyCMD 25.0 [Version 25.0.0.0] (beta build)\n"
            "Andrew Studios (C) All Rights Reserved\n\n"
            "This pyCMD Program Can Cause Damage To The System!\n"
            "Execute or search for a safe command\n\n"
            "Use the 'View' menu to split terminal panes." # New line
        )
        self.current_command_thread = None # Active command thread (global, for dialog handling)
        self.current_input_queue = None # Input queue for the active thread (global)
        self.awaiting_input = False # Global flag to know if any pane is awaiting input
        self.selected_interpreter = "cmd" # Default command interpreter selected

        # Then set up the UI
        self.setup_ui()
        if HAS_MICA:
            self.apply_mica_effect()
        self.setup_menu()
        
        # Show development warning
        self.show_development_warning()
        
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
        
        # Create first tab
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
        """)
        
    def create_new_tab(self, title="System Symbol", group_name="Default", initial_content="", initial_cwd=None, initial_interpreter=None):
        """Creates a new tab in the application, with optional initial content and group."""
        if initial_cwd is None:
            initial_cwd = self.current_directory
        if initial_interpreter is None:
            initial_interpreter = self.selected_interpreter

        # Prompt for tab title and group if not provided (for user-initiated new tab)
        if title == "System Symbol" and group_name == "Default" and not initial_content:
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

        # Create a QSplitter as the main content area for the tab
        main_splitter = QSplitter(Qt.Horizontal) # Start with horizontal split
        tab_layout.addWidget(main_splitter)

        # Create the initial terminal pane
        initial_pane = self._create_terminal_pane()
        initial_pane.group_name = group_name # Assign group to the pane
        main_splitter.addWidget(initial_pane)
        
        # Set initial content if provided (for duplicated tabs)
        if initial_content:
            initial_pane.output_text.setText(initial_content)
        
        # Add tab with group name prefix
        full_tab_title = f"[{group_name}] {title}" if group_name != "Default" else title
        tab_index = self.tab_widget.addTab(tab, full_tab_title)
        self.tab_widget.setCurrentIndex(tab_index)
        
        # Set welcome message for the initial pane if no initial content was provided
        if not initial_content:
            initial_pane.output_text.setText(self.welcome_message) 

        # Set focus on the initial pane's command entry
        initial_pane.command_entry.setFocus()
        
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
        else:
            # Fallback if no pane is focused (e.g., on initial load)
            self.show_native_message("Interpreter Change", f"Interpreter set to: {self.selected_interpreter.upper()}")
    
    def handle_command_input(self, pane_instance):
        """Handles user input from a specific pane's QLineEdit."""
        command_entry = pane_instance.command_entry
        output_text = pane_instance.output_text
        user_input = command_entry.text().strip()
        
        # If the specific pane is awaiting input, send it to its thread
        if pane_instance.awaiting_input and pane_instance.command_thread:
            pane_instance.append_output(f"<span style='color:#00FF00;'>{user_input}</span>\n", QColor(0, 255, 0)) # Show input in green
            pane_instance.send_input_to_command(user_input)
            pane_instance.set_awaiting_input(False)
            command_entry.setPlaceholderText("Enter command...")
            command_entry.setEnabled(True)
        else:
            # If not awaiting input, execute a new command in THIS pane
            self.execute_command_in_pane(pane_instance, user_input)
        
        command_entry.clear()
        output_text.moveCursor(QTextCursor.End)

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
        """Closes a tab"""
        if self.tab_widget.count() > 1:
            widget = self.tab_widget.widget(index)
            # Recursively stop all threads in all panes within this tab
            self._stop_all_pane_threads(widget)
            self.tab_widget.removeTab(index)
        else:
            QMessageBox.warning(self, "Warning", "At least one tab must remain open.")

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
            self, "Rename Tab Group", "Enter new group name:",
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

    def duplicate_tab(self, index):
        """Duplicates the selected tab."""
        source_widget = self.tab_widget.widget(index)
        
        # Get the primary pane from the source tab
        main_splitter = source_widget.layout().itemAt(0).widget()
        if isinstance(main_splitter, QSplitter):
            source_pane = main_splitter.widget(0)
        else:
            source_pane = source_widget.findChild(TerminalPane)
            if not source_pane:
                self.show_native_message("Duplication Error", "Could not find a terminal pane to duplicate.", QMessageBox.Critical)
                return

        # Extract data from the source pane
        source_title = self.tab_widget.tabText(index)
        # Remove group prefix from title for duplication
        title_match = re.match(r'\[(.*?)\]\s*(.*)', source_title)
        if title_match:
            base_title = title_match.group(2)
        else:
            base_title = source_title

        source_content = source_pane.output_text.toPlainText()
        source_cwd = self.current_directory # Assuming CWD is global for now
        source_interpreter = self.selected_interpreter # Assuming interpreter is global for now
        source_group = source_pane.group_name

        # Create a new tab with duplicated content and properties
        new_tab_title = f"Copied - {base_title}"
        self.create_new_tab(
            title=new_tab_title,
            group_name=source_group,
            initial_content=source_content,
            initial_cwd=source_cwd,
            initial_interpreter=source_interpreter
        )

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
        <b>pyCMD 25.0.0.0 Changelog:</b>
        <ul>
            <li>New UI</li>
            <li>Better Tabs (Rounded and Movable)</li>
            <li>Better Messagebox</li>
            <li>Better Textbox</li>
            <li><b>Interactive CMD Output (Dialog for Prompts)</b></li>
            <li><b>Optimized Command Execution (Background Process)</b></li>
            <li><b>Administrator Mode (Windows UAC Integration)</b></li>
            <li><b>Open files via command line argument</b></li>
            <li><b>Interpreter Selection (CMD/PowerShell/pyCMD)</b></li>
            <li><b>Split Terminal Functionality (Horizontal/Vertical)</b></li>
            <li><b>Floating Tab Style</b></li>
            <li><b>Tab Groups (Conceptual)</b></li>
            <li><b>Duplicate Tab Functionality</b></li>
            <li>And More</li>
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
            <p>Version 25.0.0.0 (Stable Build)</p>
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
    
    def execute_command_in_pane(self, pane_instance, command):
        """Executes a command within a specific TerminalPane."""
        output_text = pane_instance.output_text
        command_entry = pane_instance.command_entry

        if not command:
            return
        
        self.command_history.append(command)
        # Add a new line after the command so output starts on a new line
        pane_instance.append_output(f"> {command}\n", QColor(255, 255, 255)) # Use pane_instance.append_output for consistency
        
        # Stop any previous command thread for THIS pane
        if pane_instance.command_thread and pane_instance.command_thread.isRunning():
            pane_instance.stop_pane_thread()

        # Flag to check if an internal pyCMD command was handled
        command_handled_internally = False

        # Custom pyCMD commands (these are always handled internally)
        if command.lower().startswith("pycmd echocolor="):
            self.handle_echocolor(command, pane_instance) # Pass pane_instance
            command_handled_internally = True
        elif command.lower() == "cls":
            output_text.clear()
            output_text.setText(self.welcome_message)
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
            self.execute_rcmd_file()
            command_handled_internally = True
        elif command.lower() == "pycmd admin_only_command": # Example of an admin-only command
            if self.is_admin_mode:
                pane_instance.append_output("<span style='color: yellow;'>[ADMIN MODE] Executing sensitive operation...</span>\n", QColor(255, 255, 0))
                # Admin command logic would go here
            else:
                pane_instance.append_output("<span style='color: red;'>Access Denied: This command requires Administrator privileges.</span>\n", QColor(255, 0, 0))
            command_handled_internally = True
        
        # If the command was not handled by an internal pyCMD command
        if not command_handled_internally:
            if self.selected_interpreter == "pycmd":
                # If "pyCMD" interpreter is selected and it's not a recognized internal command
                pane_instance.append_output(f"Error: Unrecognized pyCMD internal command: '{command}'\n", QColor(255, 0, 0))
            else:
                # If "Windows CMD" or "PowerShell" interpreter is selected, execute via subprocess
                pane_instance.start_command_execution(command, self.current_directory, self.selected_interpreter)

        output_text.moveCursor(QTextCursor.End)
        command_entry.clear()
    
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
        
        user_input, ok = QInputDialog.getText(
            self, "Command Input Required", prompt_text, QLineEdit.Normal, ""
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
        pane_instance.append_output(f"\nCommand finished with exit code {return_code}\n", QColor(100, 100, 255))
        pane_instance.stop_pane_thread() # Clean up thread for this pane
        pane_instance.command_entry.setPlaceholderText("Enter command...")
        pane_instance.command_entry.setEnabled(True)
        pane_instance.command_entry.setFocus()
        pane_instance.output_text.moveCursor(QTextCursor.End)

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
        except Exception as e:
            pane_instance.append_output(f"Error executing echocolor command: {e}\n", QColor(255, 0, 0)) # Use pane_instance.append_output
    
    def change_directory(self, command, pane_instance): # Changed to take pane_instance
        """Changes the current directory"""
        new_directory = command[3:].strip()
        try:
            os.chdir(new_directory)
            self.current_directory = os.getcwd()
            pane_instance.append_output(f"Directory changed to {self.current_directory}\n", QColor(0, 255, 0)) # Use pane_instance.append_output
        except Exception as e:
            pane_instance.append_output(f"Error: {e}\n", QColor(255, 0, 0)) # Use pane_instance.append_output
            self.show_native_message("Error", f"Error: {e}", QMessageBox.Critical)
        
    def execute_python_code(self, command, output_text):
        """Executes Python code entered by the user"""
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        new_stdout = StringIO()
        new_stderr = StringIO()
        sys.stdout = new_stdout
        sys.stderr = new_stderr

        try:
            # Handle 'python ' prefix for Python commands
            code_to_execute = command[7:].strip() if command.lower().startswith("python ") else command.strip()
            exec(code_to_execute, self.python_environment)
            output = new_stdout.getvalue()
            error = new_stderr.getvalue()
            if output:
                output_text.append(output, QColor(255, 255, 255))
            if error:
                output_text.append("Error: " + error, QColor(255, 0, 0))
        except Exception as e:
            output_text.append(f"Error executing Python code: {e}\n", QColor(255, 0, 0))
            self.show_native_message("Python Execution Error", f"Error executing Python code: {e}", QMessageBox.Critical)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        
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
        commands = text_edit.toPlainText().strip().split('\n')
        if commands:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save RCMD File", "", "Command Files (*.rcmd)"
            )
            if file_path:
                with open(file_path, 'w') as f:
                    for cmd in commands:
                        if cmd.strip():  # Ignore empty lines
                            f.write(cmd.strip() + "\n")
                
                current_widget = self.tab_widget.currentWidget()
                # Find the active pane to display the message
                focused_pane = self._get_focused_terminal_pane(current_widget)
                if focused_pane:
                    focused_pane.append_output(f"Commands saved to {file_path}\n", QColor(0, 255, 0))
                
                self.show_native_message("RCMD File Saved", f"RCMD file saved to {file_path}.")
                dialog.close()

    def modify_rcmd_command(self):
        """Allows the user to modify an existing RCMD file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Modify RCMD File", self.current_directory, "Command Files (*.rcmd);;All Files (*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            self.show_native_message("Error Reading File", f"Could not read RCMD file: {e}", QMessageBox.Critical)
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
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            current_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_widget)
            if focused_pane:
                focused_pane.append_output(f"RCMD file '{os.path.basename(file_path)}' modified successfully.\n", QColor(0, 255, 0))
            
            self.show_native_message("RCMD Modified", f"RCMD file '{os.path.basename(file_path)}' saved.")
            dialog.close()
        except Exception as e:
            self.show_native_message("Error Saving Changes", f"Could not save changes to RCMD file: {e}", QMessageBox.Critical)

    def execute_rcmd_file(self):
        """Executes commands from an RCMD file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open RCMD File", "", "Command Files (*.rcmd)"
        )
        if file_path:
            current_tab_widget = self.tab_widget.currentWidget()
            focused_pane = self._get_focused_terminal_pane(current_tab_widget)

            if not focused_pane:
                self.show_native_message("Error", "No active terminal pane found to execute RCMD file.", QMessageBox.Critical)
                return

            output_text = focused_pane.output_text
            command_entry = focused_pane.command_entry
            
            focused_pane.append_output(f"Opening commands from {file_path}\n", QColor(100, 100, 255))
            
            with open(file_path, 'r') as f:
                for cmd in f:
                    cmd = cmd.strip()
                    if cmd:
                        focused_pane.append_output(f"{cmd}\n", QColor(255, 255, 255))
                        command_entry.setText(cmd)
                        self.execute_command_in_pane(focused_pane, cmd) # Execute in the focused pane
    
    def save_session(self):
        """Saves the current session to a file"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Session", "", "Session Files (*.session)"
        )
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                for i in range(self.tab_widget.count()):
                    widget = self.tab_widget.widget(i)
                    # For split terminals, we need to save the content of all panes
                    self._save_pane_contents(widget, f, self.tab_widget.tabText(i))
            
            self.show_native_message("Session Saved", f"Session saved to {file_path}.")

    def _save_pane_contents(self, widget, file_handle, tab_title, pane_index=0):
        """Recursively saves the content of TerminalPanes."""
        if isinstance(widget, TerminalPane):
            content = widget.output_text.toPlainText()
            file_handle.write(f"# TAB {tab_title} - PANE {pane_index}\n{content}\n")
        elif isinstance(widget, QSplitter):
            for i in range(widget.count()):
                self._save_pane_contents(widget.widget(i), file_handle, tab_title, pane_index + i)
        elif isinstance(widget, QWidget) and widget.layout():
            for i in range(widget.layout().count()):
                item = widget.layout().itemAt(i)
                if item.widget():
                    self._save_pane_contents(item.widget(), file_handle, tab_title, pane_index + i)
    
    def open_session(self):
        """Opens a saved session from a file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Session", "", "Session Files (*.session)"
        )
        if file_path:
            # Create a new tab for the restored session
            self.create_new_tab("Restored Tab")
            current_tab_widget = self.tab_widget.currentWidget()
            # Get the initial pane of the new tab
            main_splitter = current_tab_widget.layout().itemAt(0).widget()
            initial_pane = main_splitter.widget(0)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    # For simplicity, load all content into the first pane of the new tab
                    initial_pane.output_text.setPlainText(content)
                self.show_native_message("Session Loaded", f"Session loaded from {file_path}.")
            except Exception as e:
                initial_pane.append_output(f"Error loading session: {e}\n", QColor(255, 0, 0))
                self.show_native_message("Error Loading Session", f"Error loading session: {e}", QMessageBox.Critical)
    
    def reload_app(self):
        """Reloads the application"""
        python = sys.executable
        os.execl(python, python, *sys.argv)
        
    def show_help(self):
        """Shows the help for available commands"""
        help_message = """
Available Commands:
cls - Clear screen
help - Show this help message
pyCMD save - Save current session
pyCMD open - Open a saved session
pyCMD create rcmd - Create RCMD commands
pyCMD modify rcmd - Modify an existing RCMD command
pyCMD rcmd - Execute RCMD commands
pyCMD echocolor=(*color*)=("*text*") - Colored text output
pyCMD admin_only_command - Example of an admin-only command (requires running as Administrator)
Almost ALL Windows Terminal commands are here
All PYTHON commands are here
"""
        self.show_native_message("Help", help_message)
    
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
        pycmd_menu.addAction(QIcon.fromTheme("system-restart"), "Reload Application", self.reload_app)
        pycmd_menu.addAction(QIcon.fromTheme("application-exit"), "Exit", self.close)
        
        # File Menu
        file_menu = menubar.addMenu("File")
        file_menu.setStyleSheet("")  # Native style
        file_menu.addAction(QIcon.fromTheme("document-new"), "New Tab", lambda: self.create_new_tab("System Symbol"))
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

    def split_horizontal(self):
        # This function name now corresponds to the visual effect of a horizontal dividing line (top/bottom panes)
        self.split_current_pane(Qt.Horizontal)

    def split_vertical(self):
        # This function name now corresponds to the visual effect of a vertical dividing line (left/right panes)
        self.split_current_pane(Qt.Vertical)


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

    # Handle files opened via command line arguments
    if len(sys.argv) > 1:
        file_to_execute = sys.argv[1]
        # Get the initial pane widgets from the first tab
        # Assuming the first tab always has at least one TerminalPane inside its splitter
        first_tab_widget = window.tab_widget.widget(0)
        main_splitter = first_tab_widget.layout().itemAt(0).widget() # Get the splitter
        initial_pane = main_splitter.widget(0) # Get the first pane inside the splitter

        output_text_widget = initial_pane.output_text
        command_entry_widget = initial_pane.command_entry

        if os.path.exists(file_to_execute):
            window.append_output(f"Opening file via command line: {file_to_execute}\n", QColor(150, 255, 150), initial_pane)
            # Simulate file execution by setting the text and calling execute_command_in_pane
            window.execute_command_in_pane(initial_pane, file_to_execute)
        else:
            window.append_output(f"Error: File not found: {file_to_execute}\n", QColor(255, 0, 0), initial_pane)

    sys.exit(app.exec())
