# Contributor and agent guidance

- Use Python 3.12.x (64-bit) with Tkinter for development and PyInstaller builds targeting WinPE. The packaged EXE contains Python; WinPE does not need a separate Python installation.
- Keep `pyproject.toml` and `src/winpe_deploy/__init__.py` version values aligned. Rebuild the EXE after changing application code or the version.
- Keep the media directory name in `src/winpe_deploy/media_content.py` and `scripts/create-winpe-media.bat` aligned. `winpe-media-content/` holds boot-media content, not Python source.
- Never add active `winpe-media-content/startup-config.ini`, vendor `winpe-media-content/Drivers/`, credentials, ISO images, or generated EXEs to Git. Check staged files before publication.
- Treat disk selection, DiskPart clean, WIM application, and automatic deployment as destructive. Preserve fail-closed checks protecting boot/configuration media and local WIM storage; test changes with mocked OS commands before testing on disposable WinPE hardware.
- Run `py -3.12 -m unittest discover -s tests -v` for source changes. WinPE/UEFI behavior still requires testing on a VM and representative hardware; unit tests alone do not verify firmware boot entries.
- Add comments for non-obvious safety invariants or WinPE-specific behavior, not for self-explanatory code. Do not publish conversation transcripts, prompts, private configuration, or work-session notes.