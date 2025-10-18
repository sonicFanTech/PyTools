#!/usr/bin/env python3
"""
pybuilder.py - Multi-Python .py -> .exe builder (interactive + CLI)
Designed to run with Python 3.13.9 (works with modern Python on Windows).
Features:
 - Auto-detect installed Python interpreters (registry + common dirs + PATH)
 - Interactive prompt (compiler>) and command-line mode
 - Build .py -> .exe using PyInstaller under chosen interpreter
 - Choose target architecture (x86/x64) by selecting appropriate interpreter
 - Auto-install PyInstaller for chosen interpreter (silent unless -c passed)
 - Download & run official Python installer for requested version (silent / quiet)
 - Commands: help/?, list, active, build, install_py, exit, quit
Usage examples:
  python pybuilder.py                -> interactive prompt
  python pybuilder.py build my.py --python 3.8 --arch x86 --icon C:\icon.ico
  python pybuilder.py list
  python pybuilder.py active
"""
from __future__ import annotations
import sys

import sys, os, msvcrt
if not sys.stdin:
    sys.stdin = open('CONIN$', 'r')
if not sys.stdout:
    sys.stdout = open('CONOUT$', 'w')
if not sys.stderr:
    sys.stderr = open('CONOUT$', 'w')


import os
import shutil
import subprocess
import argparse
import platform
import tempfile
import time
import glob
import json
import urllib.request
from pathlib import Path
from typing import Dict, Tuple, Optional, List

# Windows-specific imports guarded
IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    try:
        import winreg  # type: ignore
    except Exception:
        winreg = None  # best-effort


# ---------- Utilities ----------
def debug_print(msg: str):
    # Simple debug; can be extended
    print(msg)


def run_silent(cmd: List[str], show_output: bool = False, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run subprocess; return (rc, stdout, stderr). If show_output True, stream to console."""
    if show_output:
        proc = subprocess.Popen(cmd, cwd=cwd)
        rc = proc.wait()
        return rc, "", ""
    else:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True)
        out, err = p.communicate()
        return p.returncode, out, err


def which_all(name: str) -> List[Path]:
    """Return all matches of an executable name in PATH."""
    res = []
    paths = os.environ.get("PATH", "").split(os.pathsep)
    exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep) if IS_WINDOWS else [""]
    for p in paths:
        try:
            for ext in exts:
                candidate = Path(p) / (name + ext)
                if candidate.exists():
                    res.append(candidate)
        except Exception:
            pass
    # unique
    uniq = []
    for p in res:
        if p not in uniq:
            uniq.append(p)
    return uniq


# ---------- Python discovery ----------
def discover_python_from_registry() -> Dict[str, Path]:
    """Try to read installed Pythons from Windows registry. returns dict key->exe_path"""
    found: Dict[str, Path] = {}
    if not IS_WINDOWS or winreg is None:
        return found
    # locations to check
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Python\PythonCore"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\PythonCore"),
    ]
    for root, sub in roots:
        try:
            with winreg.OpenKey(root, sub) as k:
                i = 0
                while True:
                    try:
                        ver = winreg.EnumKey(k, i)
                        i += 1
                        try:
                            with winreg.OpenKey(k, ver + r"\InstallPath") as ip:
                                instpath, _ = winreg.QueryValueEx(ip, "")
                                exe = Path(instpath) / "python.exe"
                                if exe.exists():
                                    found[ver] = exe
                        except Exception:
                            continue
                    except OSError:
                        break
        except Exception:
            continue
    return found


def discover_python_from_common_paths() -> Dict[str, Path]:
    """Search common directories for python installs."""
    cand_dirs = []
    # Common roots
    cand_dirs += glob.glob(r"C:\Python*")
    cand_dirs += glob.glob(r"C:\Program Files\Python*")
    cand_dirs += glob.glob(r"C:\Program Files (x86)\Python*")
    # Also look at Program Files subfolders
    cand_dirs += glob.glob(r"C:\Program Files\*\Python*")
    cand_dirs += glob.glob(r"C:\Program Files (x86)\*\Python*")
    found: Dict[str, Path] = {}
    for d in cand_dirs:
        exe = Path(d) / "python.exe"
        if exe.exists():
            # try to inspect version
            info = get_python_info(exe)
            ver = info[0] or exe.parent.name
            found[ver] = exe
    return found


def discover_python_in_path() -> Dict[str, Path]:
    """Look for python.exe in PATH."""
    found: Dict[str, Path] = {}
    matches = which_all("python")
    for m in matches:
        try:
            info = get_python_info(m)
            ver = info[0] or str(m)
            found[ver] = m
        except Exception:
            found[str(m)] = m
    return found


def get_python_info(python_exe: Path) -> Tuple[Optional[str], Optional[int]]:
    """Return (version_str, bits) by invoking the interpreter."""
    try:
        cmd = [str(python_exe), "-c", "import platform,struct;print(platform.python_version());print(struct.calcsize('P')*8)"]
        rc, out, err = run_silent(cmd, show_output=False)
        if rc == 0 and out:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) >= 2:
                version = lines[0]
                bits = int(lines[1])
                return version, bits
    except Exception:
        pass
    return None, None


def discover_all_pythons() -> Dict[str, Path]:
    """Aggregate discovery methods and return mapping key->exe path.
    Key format: {version} ({bits}bit) @ path"""
    mapping: Dict[str, Path] = {}
    # registry
    try:
        reg = discover_python_from_registry()
        for ver, p in reg.items():
            info = get_python_info(p)
            key = f"{info[0] or ver} ({info[1] or '?'}bit) - {p}"
            mapping[key] = p
    except Exception:
        pass
    # common
    try:
        common = discover_python_from_common_paths()
        for ver, p in common.items():
            info = get_python_info(p)
            key = f"{info[0] or ver} ({info[1] or '?'}bit) - {p}"
            mapping.setdefault(key, p)
    except Exception:
        pass
    # PATH
    try:
        pathp = discover_python_in_path()
        for ver, p in pathp.items():
            info = get_python_info(p)
            key = f"{info[0] or ver} ({info[1] or '?'}bit) - {p}"
            mapping.setdefault(key, p)
    except Exception:
        pass
    # Lastly, add well-known user-provided places if exist
    well_known = [
        Path(r"C:\Program Files\Python313\python.exe"),
        Path(r"C:\Program Files (x86)\Python38-32\python.exe"),
        Path(r"C:\Python27\python.exe"),
    ]
    for p in well_known:
        if p.exists():
            info = get_python_info(p)
            key = f"{info[0] or str(p)} ({info[1] or '?'}bit) - {p}"
            mapping.setdefault(key, p)
    return mapping


# ---------- PyInstaller & build helpers ----------
def ensure_pyinstaller(python_exe: Path, show_install_output: bool = False) -> bool:
    """Ensure PyInstaller is installed for this interpreter. If not, pip install it.
    Returns True on success."""
    # check if importable
    try:
        rc, out, err = run_silent([str(python_exe), "-c", "import PyInstaller;print(PyInstaller.__version__)"], show_output=False)
        if rc == 0:
            return True
    except Exception:
        pass
    # Not present -> install
    print("Installing PyInstaller for", python_exe)
    pip_cmd = [str(python_exe), "-m", "pip", "install", "pyinstaller"]
    if show_install_output:
        rc, _, _ = run_silent(pip_cmd, show_output=True)
        return rc == 0
    else:
        print("(silent) Installing PyInstaller... (this may take a minute)")
        rc, out, err = run_silent(pip_cmd, show_output=False)
        if rc != 0:
            print("pip install failed:", err.strip()[:200])
            return False
        return True


def build_with_pyinstaller(python_exe: Path,
                           script_path: Path,
                           icon_path: Optional[Path] = None,
                           onefile: bool = True,
                           noconsole: bool = True,
                           name: Optional[str] = None,
                           show_build_output: bool = False,
                           clean: bool = True) -> Tuple[bool, Path]:
    """
    Run PyInstaller under `python_exe` to bundle script_path.
    Returns (success, path_to_exe_if_successful)
    """
    if not script_path.exists():
        print("Script not found:", script_path)
        return False, Path()
    args = [str(python_exe), "-m", "PyInstaller"]
    if clean:
        args.append("--clean")
    if onefile:
        args.append("--onefile")
    if noconsole:
        args.append("--noconsole")
    if icon_path:
        args += ["--icon", str(icon_path)]
    if name:
        args += ["--name", name]
    args.append(str(script_path))
    print("Running PyInstaller (this can take a while)...")
    rc, out, err = run_silent(args, show_output=show_build_output)
    if rc != 0:
        if show_build_output:
            print("PyInstaller returned non-zero exit code:", rc)
        else:
            print("Build failed. Use -c / -BC to see console output.")
            if out:
                print("STDOUT (truncated):", out.strip()[:400])
            if err:
                print("STDERR (truncated):", err.strip()[:800])
        return False, Path()
    # find exe in dist/
    dist_dir = Path.cwd() / "dist"
    exe_name = (name or script_path.stem) + ".exe"
    exe_path = dist_dir / exe_name
    if exe_path.exists():
        print("Build succeeded:", exe_path)
        return True, exe_path
    else:
        # maybe different name; scan
        matches = list(dist_dir.glob("*.exe")) if dist_dir.exists() else []
        if matches:
            print("Build succeeded (found exe):", matches[0])
            return True, matches[0]
    print("Build completed but .exe not found in dist/.")
    return False, Path()


# ---------- Python installer download & run ----------
PYTHON_DOWNLOAD_BASE = "https://www.python.org/ftp/python"

def try_construct_installer_urls(version: str) -> List[str]:
    """Return possible installer URLs to try for a given version."""
    candidates = []
    # try common naming patterns for windows executable installers
    # 64-bit: python-{version}-amd64.exe
    candidates.append(f"{PYTHON_DOWNLOAD_BASE}/{version}/python-{version}-amd64.exe")
    # 32-bit: python-{version}.exe (often the 32-bit installer is just python-{ver}.exe) and python-{version}-win32.exe
    candidates.append(f"{PYTHON_DOWNLOAD_BASE}/{version}/python-{version}.exe")
    candidates.append(f"{PYTHON_DOWNLOAD_BASE}/{version}/python-{version}-win32.exe")
    # web installers variations (may not exist for all versions)
    candidates.append(f"{PYTHON_DOWNLOAD_BASE}/{version}/python-{version}-embed-amd64.zip")
    candidates.append(f"{PYTHON_DOWNLOAD_BASE}/{version}/python-{version}-embed-win32.zip")
    return candidates


def download_file(url: str, dest: Path, show_progress: bool = False) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            if resp.status != 200:
                return False
            total = resp.getheader("Content-Length")
            total = int(total) if total and total.isdigit() else None
            with open(dest, "wb") as f:
                downloaded = 0
                block = 8192
                while True:
                    chunk = resp.read(block)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if show_progress and total:
                        pc = (downloaded * 100) // total
                        print(f"\rDownloading... {pc}% ", end="", flush=True)
            if show_progress and total:
                print()
        return True
    except Exception as e:
        # print minimal info
        debug_print(f"download error: {e}")
        return False


def install_python_version(version: str, arch: str = "x64") -> bool:
    """
    Attempt to download and run the official Python installer for version.
    arch: 'x86' or 'x64'
    Returns True on success (installer executed and returned zero), False otherwise.
    """
    if not IS_WINDOWS:
        print("Python installer automation only supported on Windows hosts.")
        return False
    candidates = try_construct_installer_urls(version)
    tmp = Path(tempfile.gettempdir())
    for url in candidates:
        # skip embed zip choices for interactive install attempt and focus on .exe
        if not url.lower().endswith(".exe"):
            continue
        fname = url.split("/")[-1]
        dest = tmp / fname
        print("Trying to download installer:", url)
        ok = download_file(url, dest, show_progress=True)
        if not ok:
            print("Download failed for", url)
            continue
        # run installer silently
        cmd = [str(dest), "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_pip=1"]
        print("Running installer (may require admin privileges):", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, check=False)
            if proc.returncode == 0:
                print("Installer finished successfully.")
                return True
            else:
                print("Installer returned code", proc.returncode)
        except Exception as e:
            print("Failed to run installer:", e)
    print("Unable to automatically download or run a suitable installer for version", version)
    print("You may download it manually from https://www.python.org/downloads/")
    return False


# ---------- Interactive shell & CLI ----------
PROMPT = "compiler> "

def print_header():
    print()
    print(".py into .EXE builder - x64 & x86")
    print()
    print('Type "help" or "?" for a list of Commands')
    print()

def cmd_help():
    print("""Commands:
  help, ?                 Show this help text
  list                    Show discovered Python installations
  active                  Show active Python (the Python running this script)
  build <script> [opts]   Build .py -> .exe
      options:
         --python <ver_or_path>   Python identifier (version like 3.8 or a full path to python.exe)
         --arch x86|x64           Target architecture (uses interpreter that matches)
         --icon <path>            Optional icon file (.ico)
         --name <exename>         Output exe name
         -c                       Show pip install output (verbose)
         -BC                      Show build output from PyInstaller
  install_py <version> [x86|x64]   Download & install Python (host must be Windows)
  check_pyinstaller <python>  Check/install PyInstaller for chosen interpreter
  exit, quit              Exit this tool
  run <commandline>       Run a shell command (useful for advanced users)
Examples:
  build myscript.py --python 3.8 --arch x86 --icon C:\\icon.ico
  python pybuilder.py build myscript.py --python "C:\\Python38-32\\python.exe" --arch x86
""")

def list_pythons(discovered=None):
    if discovered is None:
        discovered = discover_all_pythons()
    if not discovered:
        print("No Python installations detected.")
        return
    print("Detected Python installations:")
    for i, (k, p) in enumerate(discovered.items(), 1):
        version, bits = get_python_info(p)
        print(f"  [{i}] {k}")

def show_active():
    print("Active Python (this process):", sys.executable)
    try:
        import struct
        print("Version:", platform.python_version(), f"{struct.calcsize('P')*8}bit")
    except Exception:
        pass

def resolve_python_identifier(identifier: Optional[str], preferred_arch: Optional[str] = None) -> Optional[Path]:
    """
    identifier may be None, a version string like '3.8', or a full path to python.exe, or a display key from discovery.
    preferred_arch: 'x86' or 'x64' to bias selection.
    Returns Path to python.exe or None.
    """
    discovered = discover_all_pythons()
    # If identifier is path
    if identifier:
        ip = Path(identifier)
        if ip.exists():
            return ip
    # If identifier is exact key (full key from discovered listing)
    if identifier:
        for k, p in discovered.items():
            if identifier in k or identifier in str(p):
                # bias by arch
                info = get_python_info(p)
                bits = info[1] or 0
                if preferred_arch:
                    if preferred_arch == "x86" and bits != 32:
                        continue
                    if preferred_arch == "x64" and bits != 64:
                        continue
                return p
    # If identifier is a short version like '3.8' or '3.8.0'
    if identifier:
        for k, p in discovered.items():
            if k.startswith(identifier) or identifier in k:
                info = get_python_info(p)
                bits = info[1] or 0
                if preferred_arch:
                    if preferred_arch == "x86" and bits != 32:
                        continue
                    if preferred_arch == "x64" and bits != 64:
                        continue
                return p
    # If no identifier given, try to pick a matching arch interpreter
    if preferred_arch:
        for k, p in discovered.items():
            info = get_python_info(p)
            bits = info[1] or 0
            if preferred_arch == "x86" and bits == 32:
                return p
            if preferred_arch == "x64" and bits == 64:
                return p
    # fallback: return sys.executable
    return Path(sys.executable)


def do_build(script: str, python_identifier: Optional[str], arch: Optional[str], icon: Optional[str],
             show_install_output: bool, show_build_output: bool, name: Optional[str]):
    script_p = Path(script)
    if not script_p.exists():
        print("Script not found:", script_p)
        return
    pyexe = resolve_python_identifier(python_identifier, preferred_arch=("x86" if arch == "x86" else "x64"))
    if not pyexe or not pyexe.exists():
        print("Could not locate a Python interpreter matching your request.")
        print("Use 'list' to see known installs, or specify a full path to a python.exe with --python")
        return
    print("Using interpreter:", pyexe)
    # ensure pyinstaller
    ok = ensure_pyinstaller(pyexe, show_install_output)
    if not ok:
        print("Failed to ensure PyInstaller is installed for", pyexe)
        return
    # build
    icon_p = Path(icon) if icon else None
    ok2, exe_path = build_with_pyinstaller(pyexe, script_p, icon_path=icon_p, show_build_output=show_build_output, name=name)
    if ok2:
        print("Build finished:", exe_path)
    else:
        print("Build failed.")


def handle_cli_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(prog="pybuilder", add_help=False)
    subparsers = parser.add_subparsers(dest="cmd")
    p_build = subparsers.add_parser("build", help="Build a script")
    p_build.add_argument("script")
    p_build.add_argument("--python", dest="python", default=None)
    p_build.add_argument("--arch", dest="arch", choices=("x86", "x64"), default=None)
    p_build.add_argument("--icon", dest="icon", default=None)
    p_build.add_argument("--name", dest="name", default=None)
    p_build.add_argument("-c", action="store_true", dest="show_pip")
    p_build.add_argument("-BC", action="store_true", dest="show_build")
    p_list = subparsers.add_parser("list", help="List discovered Pythons")
    p_active = subparsers.add_parser("active", help="Show active Python")
    p_install = subparsers.add_parser("install_py", help="Download & install Python version")
    p_install.add_argument("version")
    p_install.add_argument("arch", nargs="?", choices=("x86", "x64"), default="x64")
    p_help = subparsers.add_parser("help", help="Show help")
    args, rest = parser.parse_known_args(argv)
    if args.cmd == "build":
        do_build(args.script, args.python, args.arch, args.icon, args.show_pip, args.show_build, args.name)
        return True
    elif args.cmd == "list":
        list_pythons()
        return True
    elif args.cmd == "active":
        show_active()
        return True
    elif args.cmd == "install_py":
        install_python_version(args.version, args.arch)
        return True
    else:
        return False


def interactive_shell():
    print_header()
    discovered_cached = discover_all_pythons()
    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        import shlex
        parts = shlex.split(line)
        if not parts:
            continue
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd in ("exit", "quit"):
            break
        if cmd in ("help", "?"):
            cmd_help()
            continue
        if cmd == "list":
            discovered_cached = discover_all_pythons()
            list_pythons(discovered_cached)
            continue
        if cmd == "active":
            show_active()
            continue
        if cmd == "build":
            # naive parsing for interactive build:
            # build <script> [--python X] [--arch x86|x64] [--icon path] [-c] [-BC] [--name NAME]
            script = None
            python_id = None
            arch = None
            icon = None
            show_pip = False
            show_build = False
            name = None
            i = 0
            while i < len(args):
                a = args[i]
                if script is None and not a.startswith("-"):
                    script = a
                    i += 1
                    continue
                if a in ("--python",):
                    i += 1
                    python_id = args[i] if i < len(args) else None
                elif a == "--arch":
                    i += 1
                    arch = args[i] if i < len(args) else None
                elif a == "--icon":
                    i += 1
                    icon = args[i] if i < len(args) else None
                elif a == "-c":
                    show_pip = True
                elif a == "-BC":
                    show_build = True
                elif a == "--name":
                    i += 1
                    name = args[i] if i < len(args) else None
                i += 1
            if not script:
                print("Usage: build <script> [--python <ver_or_path>] [--arch x86|x64] [--icon <path>] [-c] [-BC]")
                continue
            do_build(script, python_id, arch, icon, show_pip, show_build, name)
            continue
        if cmd == "install_py":
            if not args:
                print("Usage: install_py <version> [x86|x64]")
                continue
            ver = args[0]
            arch = args[1] if len(args) > 1 else "x64"
            install_python_version(ver, arch)
            continue
        if cmd == "check_pyinstaller":
            # usage: check_pyinstaller <python identifier>
            if not args:
                print("Usage: check_pyinstaller <python>")
                continue
            py = resolve_python_identifier(args[0])
            if not py:
                print("Interpreter not found")
                continue
            ok = ensure_pyinstaller(py, show_install_output=True)
            print("PyInstaller present:" if ok else "PyInstaller not installed / failed to install")
            continue
        if cmd == "run":
            if not args:
                print("Usage: run <commandline>")
                continue
            shell_cmd = " ".join(args)
            print("Running:", shell_cmd)
            subprocess.run(shell_cmd, shell=True)
            continue
        if cmd == "help":
            cmd_help()
            continue
        print("Unknown command. Type 'help' or '?' for commands.")

# ---------- main ----------
def main():
    # If user invoked with args, attempt to run CLI mode
    if len(sys.argv) > 1:
        handled = handle_cli_args(sys.argv[1:])
        if handled:
            return
    # otherwise interactive
    interactive_shell()


if __name__ == "__main__":
    main()
