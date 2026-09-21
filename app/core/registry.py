import os
import sys
import winreg
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def register_magnet_protocol() -> None:
    """Register the application to handle magnet: URIs on Windows."""
    if sys.platform != "win32":
        return

    try:
        # Check if we are running as an executable or from a python script
        if getattr(sys, 'frozen', False):
            # Running as bundled executable (e.g. PyInstaller)
            exe_path = sys.executable
            command = f'"{exe_path}" "%1"'
        else:
            # Running as python script
            exe_path = sys.executable
            # Resolves to PeerStream/peerstream/main.py
            base_dir = Path(__file__).resolve().parent.parent.parent
            main_script = base_dir / "main.py"
            command = f'"{exe_path}" "{main_script}" "%1"'

        key_path = r"Software\Classes\magnet"
        
        # Create or open the key in HKEY_CURRENT_USER
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:magnet protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            
            with winreg.CreateKey(key, r"DefaultIcon") as icon_key:
                winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{exe_path}",1')
                
            with winreg.CreateKey(key, r"shell\open\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
                
        logger.debug("Magnet URI protocol registered successfully.")
    except Exception as e:
        logger.warning("Failed to register magnet protocol: %s", e)
