#!/usr/bin/env python3
"""
pybuilder.py - Multi-Python ....ive
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
  (global) --GUI           Launch GUI mode instead of CLI (pybuilder.exe --GUI)
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


# ---------- GUI (Tkinter) ----------
def launch_gui():
    """
    Simple Tkinter front-end for pybuilder.
    Offers fields for script, interpreter, arch, icon, and options,
    then calls the same build logic used by the CLI.
    """
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except Exception as e:
        print("Tkinter GUI not available:", e)
        print("Falling back to CLI.")
        interactive_shell()
        return

    root = tk.Tk()
    root.title("pybuilder - .py to .exe builder")

    # State
    state = {"last_exe": None}
    discovered = discover_all_pythons()

    # --- helpers ---
    def log(msg: str):
        log_text.configure(state="normal")
        log_text.insert("end", msg + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def refresh_pythons():
        nonlocal discovered
        discovered = discover_all_pythons()
        keys = sorted(discovered.keys())
        python_combo["values"] = keys
        if keys:
            python_combo.current(0)
        log("Refreshed Python installations.")

    def browse_script():
        path = filedialog.askopenfilename(
            title="Select Python script",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")]
        )
        if path:
            script_var.set(path)

    def browse_icon():
        path = filedialog.askopenfilename(
            title="Select icon file",
            filetypes=[("Icon files", "*.ico"), ("All files", "*.*")]
        )
        if path:
            icon_var.set(path)

    def open_last_folder():
        exe_path = state.get("last_exe")
        if not exe_path:
            messagebox.showinfo("pybuilder", "No build has been run yet.")
            return
        try:
            if exe_path.exists():
                os.startfile(str(exe_path.parent))
            else:
                messagebox.showwarning("pybuilder", "Last-built executable no longer exists.")
        except Exception as e:
            messagebox.showerror("pybuilder", f"Failed to open folder:\n{e}")

    def do_build_gui():
        script = script_var.get().strip()
        if not script:
            messagebox.showwarning("pybuilder", "Please choose a script to build.")
            return
        script_path = Path(script)
        if not script_path.exists():
            messagebox.showerror("pybuilder", f"Script not found:\n{script_path}")
            return

        python_id = python_combo.get().strip() or None
        arch_choice = arch_var.get()
        arch = None if arch_choice == "Auto" else arch_choice
        icon_path = icon_var.get().strip() or None
        name = name_var.get().strip() or None
        show_pip = show_pip_var.get()
        show_build = show_build_var.get()

        log("")
        log("=== Build started ===")
        log(f"Script: {script_path}")
        if python_id:
            log(f"Python: {python_id}")
        if arch:
            log(f"Preferred arch: {arch}")
        if icon_path:
            log(f"Icon: {icon_path}")
        if name:
            log(f"Exe name: {name}")

        root.update_idletasks()

        pyexe = resolve_python_identifier(python_id, preferred_arch=arch)
        if not pyexe or not pyexe.exists():
            messagebox.showerror("pybuilder", "Could not locate a Python interpreter matching your request.\nUse the dropdown or refresh the list.")
            log("ERROR: Interpreter not found.")
            return

        log(f"Using interpreter: {pyexe}")
        root.update_idletasks()

        ok = ensure_pyinstaller(pyexe, show_install_output=show_pip)
        if not ok:
            messagebox.showerror("pybuilder", f"Failed to ensure PyInstaller is installed for:\n{pyexe}")
            log("ERROR: PyInstaller not available or install failed.")
            return

        icon_p = Path(icon_path) if icon_path else None
        ok2, exe_path = build_with_pyinstaller(
            pyexe,
            script_path,
            icon_path=icon_p,
            show_build_output=show_build,
            name=name
        )
        if ok2:
            state["last_exe"] = exe_path
            open_btn.configure(state="normal")
            log(f"Build finished: {exe_path}")
            messagebox.showinfo("pybuilder", f"Build succeeded:\n{exe_path}")
        else:
            log("Build failed.")
            messagebox.showerror("pybuilder", "Build failed. Check the log/console for more details.")

    # --- layout ---
    main_frame = ttk.Frame(root, padding=10)
    main_frame.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    # Script selection
    script_frame = ttk.LabelFrame(main_frame, text="Script")
    script_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    script_frame.columnconfigure(1, weight=1)

    ttk.Label(script_frame, text="Script:").grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
    script_var = tk.StringVar()
    script_entry = ttk.Entry(script_frame, textvariable=script_var)
    script_entry.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    ttk.Button(script_frame, text="Browse...", command=browse_script).grid(row=0, column=2, padx=(4, 8), pady=4)

    # Python selection
    py_frame = ttk.LabelFrame(main_frame, text="Python interpreter")
    py_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    py_frame.columnconfigure(1, weight=1)

    ttk.Label(py_frame, text="Interpreter:").grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
    python_combo = ttk.Combobox(py_frame, state="readonly")
    python_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    ttk.Button(py_frame, text="Refresh", command=refresh_pythons).grid(row=0, column=2, padx=(4, 8), pady=4)

    arch_var = tk.StringVar(value="Auto")
    ttk.Label(py_frame, text="Arch:").grid(row=1, column=0, padx=(8, 4), pady=(0, 6), sticky="w")
    arch_combo = ttk.Combobox(py_frame, state="readonly", textvariable=arch_var, values=("Auto", "x86", "x64"), width=8)
    arch_combo.grid(row=1, column=1, padx=4, pady=(0, 6), sticky="w")

    # Options
    opt_frame = ttk.LabelFrame(main_frame, text="Options")
    opt_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    opt_frame.columnconfigure(1, weight=1)

    ttk.Label(opt_frame, text="Icon (.ico):").grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
    icon_var = tk.StringVar()
    icon_entry = ttk.Entry(opt_frame, textvariable=icon_var)
    icon_entry.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    ttk.Button(opt_frame, text="Browse...", command=browse_icon).grid(row=0, column=2, padx=(4, 8), pady=4)

    ttk.Label(opt_frame, text="Exe name:").grid(row=1, column=0, padx=(8, 4), pady=4, sticky="w")
    name_var = tk.StringVar()
    name_entry = ttk.Entry(opt_frame, textvariable=name_var)
    name_entry.grid(row=1, column=1, padx=4, pady=4, sticky="ew")

    show_pip_var = tk.BooleanVar(value=False)
    show_build_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt_frame, text="Show pip install output", variable=show_pip_var).grid(row=2, column=0, columnspan=2, padx=8, pady=(4, 2), sticky="w")
    ttk.Checkbutton(opt_frame, text="Show full PyInstaller output", variable=show_build_var).grid(row=3, column=0, columnspan=2, padx=8, pady=(0, 6), sticky="w")

    # Buttons
    btn_frame = ttk.Frame(main_frame)
    btn_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    btn_frame.columnconfigure(0, weight=1)
    btn_frame.columnconfigure(1, weight=1)

    build_btn = ttk.Button(btn_frame, text="Build", command=do_build_gui)
    build_btn.grid(row=0, column=0, padx=4, pady=4, sticky="e")

    open_btn = ttk.Button(btn_frame, text="Open last build folder", command=open_last_folder, state="disabled")
    open_btn.grid(row=0, column=1, padx=4, pady=4, sticky="w")

    # Log
    log_frame = ttk.LabelFrame(main_frame, text="Log")
    log_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
    main_frame.rowconfigure(4, weight=1)
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, height=12, wrap="word")
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set, state="disabled")

    # Populate interpreters initially
    keys = sorted(discovered.keys())
    python_combo["values"] = keys
    if keys:
        python_combo.current(0)

    log("Detected Python installations:")
    if keys:
        for k in keys:
            log(f"  {k}")
    else:
        log("  (none detected - try Refresh)")

    root.minsize(720, 400)
    root.mainloop()


# ---------- main ----------
def main():
    argv = sys.argv[1:]

    # Decide whether to launch GUI or stay in CLI mode
    want_gui = False
    lower_args = [a.lower() for a in argv]
    if "--gui" in lower_args or "-g" in lower_args:
        want_gui = True
        # strip GUI flags before passing the rest to CLI / shell
        argv = [a for a in argv if a.lower() not in ("--gui", "-g")]

    # If running as a frozen EXE (PyInstaller, etc.) with no args,
    # prefer the GUI by default so double-clicking shows the window.
    if not argv and getattr(sys, "frozen", False):
        want_gui = True

    if want_gui:
        launch_gui()
        return

    # CLI mode: subcommands or interactive shell
    if argv:
        handled = handle_cli_args(argv)
        if handled:
            return
    # No CLI args handled -> classic interactive shell
    interactive_shell()


if __name__ == "__main__":
    main()
