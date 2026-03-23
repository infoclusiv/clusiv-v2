import os
import subprocess


def _candidate_paths(explicit_path=None):
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)

    local_app_data = os.getenv("LOCALAPPDATA", "")
    program_files = os.getenv("PROGRAMFILES", "")
    program_files_x86 = os.getenv("PROGRAMFILES(X86)", "")

    for base_path in (local_app_data, program_files, program_files_x86):
        if not base_path:
            continue
        candidates.append(
            os.path.join(base_path, "Google", "Chrome", "Application", "chrome.exe")
        )

    return candidates


def open_url_in_chrome(url, chrome_path=None):
    for candidate in _candidate_paths(chrome_path):
        if candidate and os.path.exists(candidate):
            subprocess.Popen([candidate, url])
            return True, None

    return False, (
        "No se encontro Google Chrome. Configure CHROME_PATH o instale Chrome en una ruta estandar."
    )