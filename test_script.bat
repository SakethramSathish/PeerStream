@echo off
set "ISCC="
if defined ISCC (
    echo iscc defined
) else (
    echo [WARNING] Inno Setup (ISCC.exe) not found. Skipping installer generation.
)
