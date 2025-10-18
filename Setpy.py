#!/usr/bin/env python3
"""
pychooser.py — user-level "active Python" manager (works on Windows, Linux, macOS)

Usage (non-interactive):
  python pychooser.py list
  python pychooser.py current
  python pychooser.py set 3.11
  python pychooser.py unset
  python pychooser.py download 3.12.2 --os windows --auto
  python pychooser.py refresh

If you run the script with no arguments, it now opens an interactive prompt:
  pychooser> list
  pychooser> set 3.11
  pychooser> current
  pychooser> help
  pychooser> exit
"""
from __future__ import annotations
import argparse
import os
import sys
import subprocess
import shutil
import platform
import glob
import stat
import urllib.request
import tempfile
import shlex
import traceback
from pathlib import Path
from typing import Dict, List, Optional

# ---------- Configuration ----------
APPNAME = "pychooser"
if platform.system() == "Windows":
    DEFAULT_USER_BIN = Path(os.environ.get("USERPROFILE", Path.home())) / APPNAME / "bin"
else:
    DEFAULT_USER_BIN = Path.home() / ".local" / APPNAME / "bin"

# ---------- Helpers & detection ----------
def common_paths() -> List[Path]:
    p = []
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        userprofile = os.environ.get("USERPROFILE", "")
        if local:
            p.append(Path(local) / "Programs" / "Python")
        p.append(Path("C:/Python*"))
        if program_files:
            p.append(Path(program_files) / "Python*")
        if program_files_x86:
            p.append(Path(program_files_x86) / "Python*")
        if userprofile:
            p.append(Path(userprofile) / "AppData" / "Local" / "Programs" / "Python")
    else:
        p += [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt"),
            Path.home() / ".pyenv" / "versions",
            Path.home() / ".local" / "bin",
        ]
    return p

def is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False

def run_get_version(exe_path: Path, timeout=3) -> Optional[str]:
    try:
        proc = subprocess.run([str(exe_path), "--version"], capture_output=True, text=True, timeout=timeout)
        out = proc.stdout.strip() or proc.stderr.strip()
        if out:
            if out.lower().startswith("python"):
                return out.split()[1]
            return out
    except Exception:
        return None
    return None

def find_on_path_candidates() -> List[Path]:
    found = []
    path_env = os.environ.get("PATH", "")
    for dirpath in path_env.split(os.pathsep):
        if not dirpath:
            continue
        try:
            pdir = Path(dirpath)
            if not pdir.exists():
                continue
            for name in pdir.iterdir():
                nm = name.name.lower()
                if nm.startswith("python") and is_executable_file(name):
                    found.append(name.resolve())
        except Exception:
            continue
    out = []
    seen = set()
    for f in found:
        s = str(f)
        if s not in seen:
            out.append(f)
            seen.add(s)
    return out

def scan_common_locations() -> List[Path]:
    candidates = []
    for base in common_paths():
        if "*" in str(base):
            for candidate in glob.glob(str(base)):
                pbase = Path(candidate)
                if pbase.is_dir():
                    for possible in ["python.exe", "python", "bin/python", "bin/python3"]:
                        pp = pbase / possible
                        if is_executable_file(pp):
                            candidates.append(pp.resolve())
        else:
            if base.exists():
                if base.is_dir():
                    for child in base.iterdir():
                        if child.name.lower().startswith("python") and is_executable_file(child):
                            candidates.append(child.resolve())
                    for child in base.glob("**/bin/python*"):
                        if is_executable_file(child):
                            candidates.append(child.resolve())
                elif is_executable_file(base):
                    candidates.append(base.resolve())
    out = []
    seen = set()
    for f in candidates:
        s = str(f)
        if s not in seen:
            out.append(f)
            seen.add(s)
    return out

def detect_pythons() -> Dict[str, Path]:
    candidates = []
    candidates.extend(find_on_path_candidates())
    candidates.extend(scan_common_locations())
    result: Dict[str, Path] = {}
    for cand in candidates:
        try:
            ver = run_get_version(cand)
            if ver:
                verstr = ver.strip()
                if verstr not in result:
                    result[verstr] = cand
        except Exception:
            continue
    if platform.system() == "Windows":
        try:
            proc = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=2)
            out = proc.stdout.strip()
            for line in out.splitlines():
                parts = line.strip().split()
                if parts:
                    pathpart = parts[-1]
                    p = Path(pathpart)
                    if is_executable_file(p):
                        v = run_get_version(p)
                        if v and v not in result:
                            result[v] = p
        except Exception:
            pass
    return result

# ---------- Shim management ----------
def ensure_user_bin(user_bin: Path) -> None:
    if not user_bin.exists():
        user_bin.mkdir(parents=True, exist_ok=True)

def make_shim_unix(target: Path, user_bin: Path) -> None:
    ensure_user_bin(user_bin)
    target = target.resolve()
    vers = run_get_version(target) or "unknown"
    major_minor = ".".join(vers.split(".")[:2]) if vers != "unknown" else None
    symlinks = [user_bin / "python", user_bin / f"python{major_minor}" if major_minor else None]
    for s in symlinks:
        if s is None:
            continue
        try:
            if s.exists() or s.is_symlink():
                s.unlink()
            s.symlink_to(target)
            s.chmod(s.stat().st_mode | stat.S_IEXEC)
        except Exception as e:
            print(f"Warning: failed to create symlink {s}: {e}")

def make_shim_windows(target: Path, user_bin: Path) -> None:
    """
    Create python.bat and python3.bat wrappers that forward arguments to the target
    Example content:
      @echo off
      "C:\\path\\to\\python.exe" %*
    """
    ensure_user_bin(user_bin)
    target = target.resolve()
    vers = run_get_version(target) or "unknown"
    major_minor = ".".join(vers.split(".")[:2]) if vers != "unknown" else None
    bat_main = user_bin / "python.bat"
    bat3 = user_bin / "python3.bat"
    content = f'@echo off\r\n"{target}" %*\r\n'
    try:
        bat_main.write_text(content, encoding="utf-8")
        bat_main.chmod(0o755)
        if major_minor:
            bat3.write_text(content, encoding="utf-8")
            bat3.chmod(0o755)
    except Exception as e:
        print(f"Warning: failed to create batch wrapper: {e}")

def add_user_bin_to_path_unix(user_bin: Path, force=False) -> None:
    profile = Path.home() / ".profile"
    export_line = f'\n# added by {APPNAME}\nexport PATH="{user_bin}:$PATH"\n'
    if str(user_bin) in os.environ.get("PATH", ""):
        return
    if not force:
        print(f"\nNOTICE: {user_bin} is not currently in your PATH.")
        print(f"To make this 'active python' take effect in new shells, add the following line to your shell startup (e.g. ~/.profile or ~/.bashrc):\n")
        print(export_line)
        return
    try:
        with profile.open("a", encoding="utf-8") as f:
            f.write(export_line)
        print(f"Appended PATH export to {profile}. Open a new terminal to use the new active Python.")
    except Exception as e:
        print(f"Failed to modify {profile}: {e}")
        print("Please add the line above to your shell startup manually.")

def add_user_bin_to_path_windows(user_bin: Path) -> None:
    current = os.environ.get("PATH", "")
    s = str(user_bin)
    if s.lower() in (current.lower()):
        print(f"{user_bin} already in PATH (current process).")
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                existing_user_path, _ = winreg.QueryValueEx(key, "PATH")
            except FileNotFoundError:
                existing_user_path = ""
    except Exception:
        existing_user_path = os.environ.get("PATH", "")
    new_user_path = s + os.pathsep + existing_user_path
    try:
        subprocess.run(["setx", "PATH", new_user_path], check=True)
        print("Updated user PATH with setx. You must open a new terminal (or log out/in) to see the change.")
    except Exception as e:
        print(f"Failed to set PATH via setx: {e}")
        print("Please add the following directory to your user PATH manually:")
        print(s)

def remove_shim(user_bin: Path) -> None:
    if not user_bin.exists():
        print("No shim folder found.")
        return
    for name in ["python", "python.bat", "python3", "python3.bat"]:
        p = user_bin / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
                print(f"Removed {p}")
        except Exception as e:
            print(f"Failed to remove {p}: {e}")

# ---------- Download / install helpers ----------
def download_file(url: str, dest: Path) -> None:
    print(f"Downloading {url} -> {dest}")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
        dest.write_bytes(data)
    print("Download finished.")

def download_installer(version: str, os_target: Optional[str], dest_dir: Path) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = "https://www.python.org/ftp/python"
    if os_target == "windows":
        url = f"{base}/{version}/python-{version}-amd64.exe"
        dest = dest_dir / f"python-{version}-amd64.exe"
        try:
            download_file(url, dest)
            return dest
        except Exception as e:
            print(f"Failed to download Windows installer: {e}")
            try:
                url2 = f"{base}/{version}/python-{version}-win32.exe"
                dest2 = dest_dir / f"python-{version}-win32.exe"
                download_file(url2, dest2)
                return dest2
            except Exception:
                return None
    else:
        url = f"{base}/{version}/Python-{version}.tgz"
        dest = dest_dir / f"Python-{version}.tgz"
        try:
            download_file(url, dest)
            return dest
        except Exception as e:
            print(f"Failed to download source tarball: {e}")
            return None

def run_installer(file_path: Path, os_target: Optional[str]) -> bool:
    try:
        if platform.system() == "Windows" and file_path.suffix.lower() == ".exe":
            cmd = [str(file_path), "/quiet", "InstallAllUsers=0", "PrependPath=1"]
            print(f"Launching: {' '.join(cmd)}")
            subprocess.run(cmd, check=True)
            return True
        else:
            print(f"Downloaded installer/source at {file_path}. Please follow the platform-specific instructions to build/install.")
            return False
    except Exception as e:
        print(f"Failed to run installer: {e}")
        return False

# ---------- CLI command handlers ----------
def list_command(args):
    detected = detect_pythons()
    if not detected:
        print("No Python installations detected.")
        return
    print("Detected Python installations:")
    for ver, path in sorted(detected.items(), key=lambda kv: kv[0], reverse=True):
        print(f"  {ver:10}  -> {path}")

def current_command(args):
    user_bin = Path(args.user_bin) if getattr(args, "user_bin", None) else DEFAULT_USER_BIN
    if platform.system() == "Windows":
        p = user_bin / "python.bat"
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="ignore")
            import re
            m = re.search(r'"([^"]+python(?:\.exe)?)"', txt, re.IGNORECASE)
            if m:
                target = Path(m.group(1))
                ver = run_get_version(target) or "<unknown>"
                print(f"Active (shim): python -> {target} (version {ver})")
                return
    else:
        p = user_bin / "python"
        if p.exists() and p.is_symlink():
            try:
                target = p.resolve()
                ver = run_get_version(target) or "<unknown>"
                print(f"Active (shim): python -> {target} (version {ver})")
                return
            except Exception as e:
                print(f"Error resolving shim: {e}")
                return
    print("No active shim found created by pychooser.")

def set_command(args):
    desired = args.target
    user_bin = Path(args.user_bin) if getattr(args, "user_bin", None) else DEFAULT_USER_BIN
    detected = detect_pythons()
    target_path: Optional[Path] = None
    p_candidate = Path(desired)
    if p_candidate.exists() and is_executable_file(p_candidate):
        target_path = p_candidate.resolve()
    else:
        for ver, path in detected.items():
            if ver.startswith(desired):
                target_path = path
                break
    if target_path is None:
        print(f"Could not find a Python matching '{desired}'. Run 'list' to see detected installs.")
        return
    print(f"Setting active Python to {target_path}")
    if platform.system() == "Windows":
        make_shim_windows(target_path, user_bin)
        add_user_bin_to_path_windows(user_bin)
        print(f"Created wrappers in {user_bin}. Open a new terminal to use the new 'python'.")
    else:
        make_shim_unix(target_path, user_bin)
        if str(user_bin) not in os.environ.get("PATH", ""):
            if getattr(args, "force_path", False):
                add_user_bin_to_path_unix(user_bin, force=True)
            else:
                add_user_bin_to_path_unix(user_bin, force=False)
        print(f"Created symlink(s) in {user_bin}. Open a new terminal to use the new 'python'.")

def unset_command(args):
    user_bin = Path(args.user_bin) if getattr(args, "user_bin", None) else DEFAULT_USER_BIN
    remove_shim(user_bin)
    print("Unset completed. Note: PATH changes (if any) are not automatically reverted.")

def download_command(args):
    version = args.version
    os_target = args.os_target
    dest_dir = Path(args.dest) if getattr(args, "dest", None) else Path(tempfile.gettempdir()) / "pychooser_downloads"
    print(f"Downloading Python {version} for '{os_target or platform.system().lower()}' into {dest_dir}")
    path = download_installer(version, os_target, dest_dir)
    if not path:
        print("Download failed or not available for that platform/version.")
        return
    if getattr(args, "auto", False):
        ok = run_installer(path, os_target)
        if ok:
            print("Installer launched. If it succeeded, run 'pychooser.py refresh' to detect the new installation.")
        else:
            print("Installer wasn't launched automatically; please run it manually.")
    else:
        print(f"Saved to: {path}")
        print("Run the installer manually or re-run with --auto to attempt to run it.")

def refresh_command(args):
    print("Refreshing detected Python installations...")
    detected = detect_pythons()
    if not detected:
        print("No Python installations detected.")
        return
    print("Detected the following after refresh:")
    for ver, path in sorted(detected.items(), key=lambda kv: kv[0], reverse=True):
        print(f"  {ver:10} -> {path}")

# ---------- Parser / interactive shell ----------
def create_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pychooser", description="Manage user-level active Python version")
    ap.add_argument("--user-bin", help=f"Override user bin directory (default: {DEFAULT_USER_BIN})")
    sub = ap.add_subparsers(dest="cmd", required=False)

    sub_list = sub.add_parser("list", help="List detected Python installations")
    sub_list.set_defaults(func=list_command)

    sub_cur = sub.add_parser("current", help="Show which python shim created by this tool is active")
    sub_cur.set_defaults(func=current_command)

    sub_set = sub.add_parser("set", help="Set active Python version (pass version prefix like '3.11' or full path)")
    sub_set.add_argument("target", help="Version prefix (e.g. 3.11) or full path to python executable")
    sub_set.add_argument("--force-path", action="store_true", help="Automatically add user bin to PATH (Unix)")
    sub_set.set_defaults(func=set_command)

    sub_un = sub.add_parser("unset", help="Remove shim created by this tool")
    sub_un.set_defaults(func=unset_command)

    sub_dl = sub.add_parser("download", help="Download an installer or source tarball for a Python version")
    sub_dl.add_argument("version", help="Exact version string, e.g. 3.12.2")
    sub_dl.add_argument("--os", dest="os_target", choices=["windows", "unix"], help="Target OS for the download")
    sub_dl.add_argument("--auto", action="store_true", help="Try to run installer automatically (Windows only supported)")
    sub_dl.add_argument("--dest", help="Destination folder to save download")
    sub_dl.set_defaults(func=download_command)

    sub_ref = sub.add_parser("refresh", help="Rescan and show detected Python installations")
    sub_ref.set_defaults(func=refresh_command)

    return ap

def run_once(argv: Optional[List[str]] = None) -> None:
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "cmd", None) is None:
            # no command requested; show help (non-interactive) or will be handled by interactive loop
            parser.print_help()
            return
        args.func(args)
    except SystemExit:
        # argparse calls sys.exit on parse errors; convert to friendly message instead of exiting program
        print("Invalid command or invalid arguments. Type 'help' for available commands.")
    except Exception:
        print("An unexpected error occurred:")
        traceback.print_exc()

def interactive_shell() -> None:
    parser = create_parser()
    print("pychooser interactive mode. Type 'help' for usage, 'exit' or 'quit' to leave.")
    while True:
        try:
            line = input("pychooser> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.lower() in ("help", "h", "?"):
            parser.print_help()
            continue
        try:
            argv = shlex.split(line)
        except ValueError as e:
            print(f"Parse error: {e}")
            continue
        try:
            args = parser.parse_args(argv)
            if getattr(args, "cmd", None) is None:
                parser.print_help()
                continue
            args.func(args)
        except SystemExit:
            # show user-friendly usage for this subcommand
            print("Invalid command or arguments. Try 'help' to see usage.")
        except Exception:
            print("Error while running command:")
            traceback.print_exc()

def main():
    if len(sys.argv) == 1:
        # Interactive when no arguments are supplied
        interactive_shell()
    else:
        run_once(sys.argv[1:])

if __name__ == "__main__":
    main()
