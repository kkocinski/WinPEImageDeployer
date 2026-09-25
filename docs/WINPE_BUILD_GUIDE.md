# Building the WinPE Image Deployer Executable and WinPE Media

This guide creates a bootable WinPE USB drive or ISO with `WinPEImageDeployer.exe` embedded in `boot.wim`. When WinPE starts, the application runs automatically after `wpeinit` initializes Plug and Play and networking.

## 1. Technician workstation requirements

Use a 64-bit Windows technician workstation. Install:

1. **Python 3.12.x (64-bit) is mandatory**, including Tkinter. Do not use Python 3.13 or newer to build this project for WinPE: this project's Tkinter integration has compatibility problems with newer Python versions in WinPE.
2. **PyInstaller** for Python 3.12:

   ```powershell
   py -3.12 -m pip install --upgrade pyinstaller
   ```

3. The **Windows Assessment and Deployment Kit (ADK)** with **Deployment Tools**.
4. The matching **Windows PE add-on for the ADK**.

The ADK and WinPE add-on versions should match. Build WinPE `amd64` media for Intel/AMD physical computers and virtual machines, or `arm64` media only for compatible ARM devices.

## 2. Build the application executable

Open an ordinary Command Prompt or PowerShell window and run. Replace `<repo>` with the absolute path to your local clone (for example `C:\Projects\WinPEImageDeployer`):

```bat
<repo>\scripts\build-exe.bat
```

The result is a single file:

```text
<repo>\dist\WinPEImageDeployer.exe
```

The executable is created with PyInstaller in `--onefile` and `--console` mode so preflight status is visible in the WinPE command window. It contains the Python runtime, Tkinter, and the application code; Python does **not** need to be installed inside WinPE.

## 3. Create customized WinPE working files

Open **Deployment and Imaging Tools Environment** from the Start menu using **Run as administrator**. This is important because the ADK shortcut sets the required paths for `copype.cmd` and `MakeWinPEMedia.cmd`.

Run one of the following commands:

```bat
<repo>\scripts\create-winpe-media.bat amd64 C:\WinPE-Image-Deployer-amd64
```

This creates and customizes `C:\WinPE-Image-Deployer-amd64\media\sources\boot.wim` but does not create boot media yet.

To immediately create a USB drive:

```bat
<repo>\scripts\create-winpe-media.bat amd64 C:\WinPE-Image-Deployer-amd64 /UFD E:
```

To create an ISO:

```bat
<repo>\scripts\create-winpe-media.bat amd64 C:\WinPE-Image-Deployer-amd64 /ISO C:\WinPEImageDeployer.iso
```

> **Warning:** `/UFD E:` formats the USB drive assigned to `E:`. Confirm its drive letter before pressing Enter.

The application does not require the WinPE WMI, .NET, or PowerShell optional components for normal use or automatic deployment with an empty `expected_disk_serial`. In minimal WinPE, DiskPart is used and deployment proceeds only when exactly one sufficiently large disk remains after protecting the WinPE/configuration media. A configured `expected_disk_serial` requires PowerShell disk metadata so the serial can be verified.

### Optional drivers loaded from boot media at WinPE startup

Before making `/UFD` media or an ISO, the script copies the media-content folder `<repo>\winpe-media-content\` into the WinPE `media` tree. After `wpeinit`, and before the GUI opens, WinPE Image Deployer scans every mounted drive letter except `X:` for this folder and loads every `.inf` below its `Drivers` subfolder using `drvload.exe`.

```text
E:\winpe-media-content\Drivers
```

Replace `E:` with the USB letter selected for `/UFD`. Copy extracted vendor driver packages (`.inf` files, including their accompanying catalog and binary files) into this folder or its subfolders. At application startup, WinPE Image Deployer loads them with `drvload.exe`, then refreshes disk and network discovery. This works from a `/UFD` USB, from a directly booted ISO, and from an ISO written to USB with Rufus, because the folder is included at the root of the WinPE media. The drivers are valid only for that WinPE boot session. CD-ROM/ISO media can provide drivers but is never eligible as an auto-deploy target.

### Optional startup Ethernet and SMB mapping configuration

The script copies all files below `<repo>\winpe-media-content\`, including `Drivers` subfolders and an active `startup-config.ini` when present. The content is included in `/UFD` media and generated ISO files, including an ISO written to USB by Rufus. Copy or rename the repository template to `<repo>\winpe-media-content\startup-config.ini` to enable optional startup configuration. Before either deployment or GUI startup, the application loads drivers, waits 10 seconds for newly loaded hardware to initialize, applies `[ethernet]` static IPv4 when configured, and maps `[share]`. When static IPv4 succeeds and a share is configured, it waits another 10 seconds before the first SMB attempt. SMB mapping is retried up to 6 times at 5-second intervals. If more than one active `startup-config.ini` is found on mounted media, startup configuration is skipped rather than guessing.

The pre-GUI actions always write an independent diagnostic log to `X:\Windows\Temp\WinPEImageDeployer-startup.log`. It records all scanned drive letters, discovered `winpe-media-content` folders, number of `.inf` files, every `drvload` command/result, driver initialization wait, active configuration detection, available network adapters, Ethernet configuration, SMB attempts, and any errors. This log is available even if the GUI cannot start.

Before any destructive action, preflight records protected disk numbers, detected physical disks, the selected target, deployment status, and any failure reason in the same log and in the WinPE command window. The **Text Editor** tab can open, edit, and save `startup-config.ini` or other local text files; restart WinPE after changing startup configuration so preflight reads the new values.

The file contains an intentionally plaintext password for the offline removable-media workflow. Keep the USB physically secure and use an SMB account with only the permissions required for image storage. The application redacts the password from command logs. Configuration errors, adapter absence, and SMB mapping failures are warnings only and do not prevent the GUI from opening.

In `[ethernet]`, specify `adapter = Ethernet` **or** `mac = AA-BB-CC-01-02-FF` (colon separators also work). For MAC-only selection, remove or clear `adapter`. If both are given, they must identify the same interface. When no interface or multiple interfaces match the MAC, static IPv4 is not applied; check the startup log. MAC matching uses the Windows network adapter list, not the interface name or list position.

### Optional safe auto-deploy

The same `startup-config.ini` can include `[auto_deploy]`:

**Current scope:** Automatic deployment assumes the chosen physical disk is dedicated to one Windows installation and can be erased in full. It does not require the disk to have literally one partition: UEFI deployment creates EFI, MSR, and Windows partitions, while BIOS deployment creates a Windows partition. Existing partitions, including a data partition such as D: on the same disk, are not preserved. Disk-to-disk copying and partition-to-partition copying/restoration are only being considered for the future; neither is supported by `[auto_deploy]` or the current Deploy workflow.

```ini
[auto_deploy]
enabled = false
wim_path = Z:\Images\Windows11.wim
image_index = 1
firmware = UEFI (GPT)
minimum_disk_size_gib = 100
maximum_disk_size_gib =
expected_disk_serial =
```

Set `enabled = true` only after validating the hardware layout. This starts destructive deployment before the GUI is created, after optional drivers, Ethernet, and SMB mapping complete. It validates that the WIM exists, confirms the configured image index through DISM, and protects the disk holding the configuration and any local WIM. Optional `maximum_disk_size_gib` is an inclusive upper size bound in GiB; leave it empty for no upper bound. For example, `1100` permits a 1 TB disk (~931 GiB) but excludes a 4 TB disk (~3725 GiB). With an empty `expected_disk_serial`, exactly one unprotected disk within the size bounds must remain; this works in minimal WinPE through DiskPart. With a serial configured, PowerShell metadata is required and exactly one unprotected disk within the size bounds must match the serial exactly, even if other disks are present. The selected disk is cleaned in its entirety; this mode does not preserve any partition on it. After successful deployment and boot-file creation, WinPE restarts with `wpeutil Reboot`. Any validation or deployment failure is logged and opens the GUI instead.

Automatic deployment is deliberately fail-closed: the disk containing the configuration is protected; a disk containing a local WIM is also protected; CD-ROM/ISO media cannot be selected; the minimum and configured maximum size are enforced; and exactly one eligible target must remain. Any failure prevents auto-deploy and opens the GUI.

Before setting `expected_disk_serial`, obtain the target serial in WinPE with:

```powershell
powershell.exe -NoProfile -Command "Get-Disk | Format-Table Number,FriendlyName,BusType,SerialNumber,Size -AutoSize"
```

You can also copy the `S/N:` value displayed in the application's **Deploy** or **Physical Disks** tab. Use the serial exactly as displayed. If the hardware does not report a serial, leave it blank and use only a thoroughly validated single-target environment.

### WIM destination filesystem requirement

Store a single-file WIM on **NTFS** or **exFAT**. Do not use FAT32: FAT32 limits an individual file to 4 GiB, which can make DISM fail partway through capture with an out-of-space message even when the drive has substantial free capacity.

## 4. Add network and storage drivers

If WinPE does not detect target disks, NVMe/RAID controllers, USB controllers, or network adapters, download the appropriate **extracted INF driver packages** from the hardware vendor. Do not use an `.exe` driver installer directly; extract it first.

With the WinPE working directory already created, run from an elevated Command Prompt:

```bat
<repo>\scripts\add-winpe-drivers.bat C:\WinPE-Image-Deployer-amd64 C:\Drivers
```

After driver injection, recreate the USB or ISO:

```bat
MakeWinPEMedia /UFD C:\WinPE-Image-Deployer-amd64 E:
```

or:

```bat
MakeWinPEMedia /ISO C:\WinPE-Image-Deployer-amd64 C:\WinPEImageDeployer.iso
```

## 5. What the customization does

The customization script copies the EXE to:

```text
X:\Windows\System32\WinPEImageDeployer.exe
```

It replaces `startnet.cmd` inside `boot.wim` with:

```bat
@echo off
wpeinit
X:\Windows\System32\WinPEImageDeployer.exe --startup-preflight
cmd.exe
```

The command prompt remains available when the GUI closes. This permits manual diagnostics such as `ipconfig`, `diskpart`, `dism`, and `wpeutil reboot`.

### Disk and partition management safety

The application includes a **Disk Tools** tab for assigned volumes. It can set a volume label, replace a drive letter, or quick-format a volume as NTFS, exFAT, or FAT32. Formatting is destructive: it requires both a confirmation dialog and typing the selected letter (`E` or `E:`). The WinPE `X:` RAM disk cannot be managed. If `X:` is absent, `C:` is also protected so the tool cannot accidentally manage the running Windows system volume.

The **Physical Disks** tab exposes DiskPart-backed layout operations: list partitions, create and format a primary partition in contiguous unallocated space, delete a selected partition, extend or shrink an assigned volume, and clean an entire disk. `Clean` is deliberately protected by selecting the disk, typing its number, and accepting a final destructive prompt. It removes all partitions but cannot reconstruct an OEM recovery/factory layout without the manufacturer's recovery image.

### Folder selection in WinPE

For mount locations, driver injection, and file injection folders, the application uses its own folder browser instead of the limited native Tk folder dialog. Select a drive, double-click folders to open them, use **Up** to return to the parent, and select the displayed current folder. This avoids the blank folder tree that can appear in minimal WinPE sessions.

## 6. First boot checklist

1. Boot a non-production test computer from the WinPE USB or ISO.
2. Wait for the application window to open.
3. Open **Deploy** and check that all expected disks are visible.
4. Open **Network**, configure connectivity if DHCP is not available, and test an SMB mapping.
5. Capture and deploy a small test image before using a production reference image.
6. Verify that the deployed computer starts successfully with a firmware mode and partition style that match the application selection: `UEFI (GPT)` for UEFI firmware or `BIOS (MBR)` for legacy BIOS firmware. In Hyper-V, use `UEFI (GPT)` for a Generation 2 VM and `BIOS (MBR)` for a Generation 1 VM; do not mix these modes.
7. After deployment, eject the WinPE USB/ISO before the first boot. The application bottom bar also provides confirmed Restart WinPE and Shut Down WinPE buttons.

## 7. Updating the application

When application code changes:

1. Run `build-exe.bat` again.
2. Create a **new** WinPE working directory with `create-winpe-media.bat`, or mount the existing `boot.wim` and replace `X:\Windows\System32\WinPEImageDeployer.exe` manually.
3. Recreate the USB/ISO with `MakeWinPEMedia`.

Creating a fresh working directory is the safest update method because it avoids accidental reuse of a stale DISM mount.

## Operational notes

- WinPE runs from a RAM disk (`X:`); files written there disappear at restart.
- Store WIM files, logs, and injected application builds on external storage or a mapped network share.
- The deployment operation intentionally runs `DiskPart clean` after explicit in-application confirmation. It permanently removes partitions from the selected target disk.
- Include storage and network drivers for every target physical computer or virtual hardware platform in the WinPE image for permanent support. For one-session diagnostics, place extracted packages in `<USB>:\winpe-media-content\Drivers` before booting, or use **Load drivers into current WinPE...**; then refresh disks or adapters after the live load.
- Test every new driver set and WinPE release on representative physical hardware and virtual machines before production deployment.