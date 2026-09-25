# WinPE Image Deployer

WinPE Image Deployer 0.2.1 is a Python 3.12 Tkinter application for capturing, servicing, storing, and deploying Windows images in Windows PE. It uses native Windows tools: **DISM**, **DiskPart**, **BCDBoot**, **netsh**, and **net use**.

> **Project status — unstable, under active development.** Version 0.2.1 is published as source code for review and further development, **not as a production-ready release**. Unit tests use mocked operating-system commands; they do not verify real disk operations, WinPE startup, or UEFI boot behavior. A complete end-to-end test of this version on representative physical WinPE hardware is **not documented in this repository**. Do not assume that publishing the repository means these workflows have passed hardware validation. Test in a VM and on disposable hardware before considering any real deployment.

> **Warning**: Deployment permanently erases the selected target disk. Review the disk number, model, and size before confirming an operation.

**Current deployment scope:** Treat one physical target disk as dedicated to one Windows installation, not as a disk whose existing partitions must be preserved. Deployment cleans the *entire* target disk and builds a new boot layout: UEFI/GPT creates EFI, MSR, and Windows partitions; BIOS/MBR creates a Windows partition. Thus "one disk, one partition" is only a shorthand for one Windows/data volume, not a literal description of the UEFI layout. A disk containing both Windows (C:) and data (D:) is **not** a supported restore target if D: must survive. The current capture operation saves one selected offline volume to a WIM; it does not clone the entire disk.

## License

Review the code and validate destructive workflows on representative hardware before use. The project source is distributed under the [MIT license](LICENSE); third-party driver packages and Windows/ADK components are **not** covered by this license.

## Features

- Capture an offline Windows volume into a single compressed `.wim` file, with Maximum (LZX), Fast (XPRESS), or no compression.
- Deploy a selected image index to a blank or existing target disk, with physical-disk model, bus type, serial number (when available), capacity, volume usage, and unallocated-space visibility.
- Create either UEFI/GPT or BIOS/MBR boot layouts.
- Apply images to disks of different capacities; DISM expands files to the available target volume.
- Inspect WIM metadata and choose indexes from `Index — Name` selectors (manual index entry remains supported).
- Service a WIM image by mounting it, copying files into it, and committing the result.
- Add extracted signed driver packages (`.inf`) from a folder and all its subfolders into a mounted WIM image.
- Load extracted `.inf` drivers into the current WinPE boot session without rebuilding or changing a WIM.
- Configure IPv4 address, gateway, and DNS for a network adapter.
- Connect or disconnect SMB shares using credentials entered only for the current session.
- Maintain an application activity log and save it to removable media or a network share.

## Requirements

- **Python 3.12.x (64-bit), including Tkinter, is required to develop and build the EXE.** Do not use Python 3.13 or newer for WinPE builds: this project's Tkinter integration has compatibility problems in newer Python versions under WinPE. The packaged EXE includes Python; no separate Python installation is needed in WinPE.
- Windows PE with:
  - the packaged `WinPEImageDeployer.exe` embedded in the image (Python is not required in WinPE);
  - `dism.exe`, `diskpart.exe`, `bcdboot.exe`, `wpeutil.exe`, `netsh.exe`, and `net.exe` available on `PATH`;
  - the DISM WIM provider and required storage/network drivers.
- Administrator privileges. Standard WinPE startup normally provides them.
- A source Windows installation already generalized with Sysprep when required by your deployment process. This program intentionally does not validate Sysprep status.

## Quick start

From the project directory, install the package first:

```powershell
py -3.12 -m pip install .
py -3.12 -m winpe_deploy.main
```

Or use the installed entry point:

```powershell
winpe-image-deployer
```

## Build the executable and bootable WinPE media

The repository contains automation for creating a self-contained EXE and embedding it in a bootable WinPE image. Run the following from the repository root:

```bat
scripts\build-exe.bat
```

Then, from an elevated **Deployment and Imaging Tools Environment** prompt:

```bat
scripts\create-winpe-media.bat amd64 C:\WinPE-Image-Deployer-amd64 /UFD E:
```

The `/UFD` operation formats the selected USB drive. For a full installation, driver-injection, ISO-creation, and test guide, read `docs/WINPE_BUILD_GUIDE.md`.

The `winpe-media-content` directory is copied to the root of the generated boot media. Put extracted drivers in its `Drivers` subdirectory and copy `startup-config.ini.example` to `startup-config.ini` only after reviewing it. **The local active configuration and vendor drivers are excluded from Git but are included in locally generated boot media.** The configuration can contain a plaintext SMB password and enable automatic disk erasure; inspect your media before sharing it. Do not publish vendor drivers without reviewing their redistribution terms.

In the optional `[ethernet]` section, identify the network card by its exact `adapter` name **or** by `mac` (for example `AA-BB-CC-01-02-FF`); both are not required. Clear `adapter` when selecting by MAC. If both are supplied they must agree, and ambiguous or missing MAC matches never configure a different card.

Automatic UEFI reboot requires an existing firmware `{bootmgr}` entry. After writing boot files the app points it at the target ESP, places it first in `{fwbootmgr}`, and verifies the reported configuration. It does not create a missing NVRAM entry. If validation fails, automatic restart is stopped; firmware-specific behavior still requires testing on representative UEFI hardware with boot media attached.

## Capture workflow

1. Boot the technician PC into WinPE.
2. Connect the prepared source disk and an image storage location.
3. On **Capture**, use **Refresh volumes**, select the offline source volume and a separate WIM destination volume from the lists, enter a WIM filename, then enter an image name and description.
4. Select **Capture image**.
5. Use **Inspect WIM** to verify the captured image index.

The resulting WIM is a single file and can be placed on USB storage, external disks, or a mapped SMB drive. `Maximum (LZX)` is the default and produces the smallest WIM, but it takes longer. The application reads free space through native Windows APIs and warns when destination free space is less than currently used source space; compression can reduce the final WIM but cannot guarantee that capture will fit. During capture, the DISM process receives `TEMP`, `TMP`, and `/ScratchDir` pointing to a temporary directory on the selected destination volume instead of WinPE's `X:` RAM disk. Store a single WIM on **NTFS** or **exFAT**, not FAT32: FAT32 has a 4 GiB per-file limit and can fail during capture despite ample remaining capacity.

## Deployment workflow

1. On **Deploy**, inspect the detected physical disks. Confirm the target disk number, model, bus type, serial number where available, total capacity, used/free volume space, and unallocated space.
2. Select the WIM file and image index.
3. Choose the target firmware type that matches the computer or virtual machine that will boot the disk: `UEFI (GPT)` for UEFI firmware and `BIOS (MBR)` for legacy BIOS firmware. In Hyper-V, this maps to **Generation 2** for UEFI/GPT and **Generation 1** for BIOS/MBR.
4. Type the target disk number in the destructive-operation confirmation field.
5. Select **Deploy image** and accept the final confirmation dialog.

Deployment runs DiskPart to clean and partition the target disk, applies the selected WIM with DISM, then runs BCDBoot with `/c` to create a fresh boot configuration. Before partitioning, the application detects occupied drive letters and dynamically selects unused temporary letters for the EFI and Windows partitions; it never uses WinPE's `X:` RAM disk. DISM scratch data is created on the new temporary Windows partition rather than `X:`. The primary Windows partition consumes remaining disk capacity, so differently sized disks are supported as long as the applied image fits. For UEFI, deployment then uses BCDEdit to point an existing Windows Boot Manager firmware entry at the new EFI partition and move it ahead of removable media in firmware priority. If that entry is absent or the UEFI boot order cannot be verified, automatic deployment does not reboot: inspect the WinPE log and firmware settings instead. Actual unattended boot still depends on the firmware honoring its reported order; verify on the intended hardware with boot media attached. BIOS deployments do not modify firmware priority. This workflow supports physical computers and virtual machines when their firmware mode, disk partition style, Windows image architecture, and required storage/network drivers are compatible.

**Possible future work (not implemented):** disk-to-disk copying of an entire physical disk, and partition-to-partition copying/restoration of a specific partition while retaining other partitions (for example, restoring Windows without erasing D:). Neither mode is available in the current GUI or automatic deployment.

At startup, verify the application title and first log entry include the expected build version. The main window starts large for the detected screen, requests maximization after its first paint, and retries once for slower WinPE window managers; if maximization is unavailable, the calculated large window is retained. Capture and deployment progress display an estimated remaining time after DISM provides enough measured percentage progress; it is a changing estimate, not a guaranteed completion time. Pre-GUI startup can load drivers, configure Ethernet, map SMB storage, and automatically deploy a WIM only when exactly one eligible target remains. In this build, `/Capture-Image` explicitly uses `/ScratchDir` on the selected WIM destination volume. The `/Apply-Image` command intentionally contains no `/ScratchDir` argument; its temporary directory is provided only through the child DISM process `TEMP` and `TMP` environment variables. WIM paths selected by the GUI are normalized to native Windows backslashes before they are passed to DISM. The bottom bar permanently reserves space for the status message and confirmed **Restart WinPE** and **Shut Down WinPE** controls.

## Image servicing workflow

1. On **Service Image**, choose a WIM and use **Load indexes** to select `Index — Name` (or enter the number manually).
2. Select a parent folder for the local mount. The built-in WinPE folder browser lists available drives and subfolders, and the application uses the selected folder's `WimMount` subfolder.
3. Select a standard destination folder inside the image or enter a validated custom relative path, such as `Install\\Packages`. The standard choices include `ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup` for all-users startup items and `Users\\Public\\Desktop` for the shared desktop.
4. Add one or more host files/directories, then select **Inject files and commit**.

The application mounts the image read/write, copies files to the image, commits changes, and unmounts it. The Service Image tab shows an activity/progress bar and the current servicing step; DISM percentage output is shown when available. If the operation fails, use **Discard mounted image** before retrying. Avoid storing the mount directory inside a source folder being copied.

### Offline WIM driver injection

On **Service Image**, select the same WIM, index, and mount parent folder. Under **Offline WIM driver injection**, use the built-in folder browser to choose a folder containing extracted driver packages (`.inf` files). The application mounts the WIM, runs DISM with `/Add-Driver /Driver:<folder> /Recurse`, commits the image, and unmounts it. This permanently modifies the selected offline WIM; it does not load a driver into the WinPE session currently running. Use vendor-provided, signed drivers. The application deliberately does not use `/ForceUnsigned`.

### Current WinPE session drivers

Use **Load drivers into current WinPE...** when a storage, USB, RAID/NVMe, or NIC driver is needed immediately. The dialog loads each selected extracted `.inf` through `drvload.exe`, then refreshes disks and adapters. This does not rebuild or modify `boot.wim`, and all drivers loaded this way are lost when WinPE restarts.

For automatic loading from boot media, create this directory and copy extracted driver packages into it (subfolders are supported):

```text
<USB drive>:\winpe-media-content\Drivers
```

The WinPE-media creation script creates this directory in the WinPE `media` tree before it creates `/UFD` media or an ISO. Therefore it works with a USB created through `/UFD`, with an ISO booted directly, and with that ISO written to USB by Rufus. At startup, the program scans assigned drives other than `X:`; CD-ROM/ISO media is accepted as a source for configuration and drivers but is never eligible as an auto-deploy target.

## Disk tools

The **Disk Tools** tab lists assigned volumes and can change a volume label, replace its drive letter, or quick-format it as NTFS, exFAT, or FAT32. Formatting erases all files and requires typing the selected letter before confirmation (`E` or `E:` are accepted). The WinPE `X:` RAM disk is protected and cannot be managed. If `X:` is not present, the application treats the environment as non-WinPE and also protects `C:` from volume-management and resize operations.

## Physical disks and partitions

The **Physical Disks** tab manages the selected physical disk through DiskPart. It can list partitions, create and format a primary partition in contiguous unallocated space, delete a selected partition, and extend or shrink an assigned volume. It also offers **Clean entire disk** to remove all partitions and data from the selected disk. Cleaning requires both the selected disk and typing its disk number, followed by a final confirmation. It creates an empty disk only; restoring an OEM/factory recovery layout requires the manufacturer-specific recovery image and is not automated.

## Networking

The **Network** tab configures IPv4 network settings through `netsh`. Select an adapter and click **Enable DHCP (IPv4 + DNS)** to return both its address and DNS servers to automatic configuration. It can map an SMB share using `net use`. DHCP changes the current adapter settings and may disconnect existing network shares; the startup configuration file is not changed, so a configured static address will be applied again on the next WinPE startup.

- Credentials are requested in a modal dialog and are held only in process memory long enough to run `net use`.
- Passwords are not written to application logs.
- Prefer a mapped drive letter (for example `Z:`) when selecting WIM files in the other tabs.

### Optional startup Ethernet, SMB, and auto-deploy configuration

To configure startup networking or deployment, save the following file on boot media:

```text
<USB drive>:\winpe-media-content\startup-config.ini
```

The media script creates `startup-config.ini.example` in that location before generating either `/UFD` media or an ISO. Edit the example and rename it to `startup-config.ini` to enable it. It supports each section independently:

```ini
[ethernet]
adapter = Ethernet
address = 192.168.1.20
mask = 255.255.255.0
gateway = 192.168.1.1
dns = 192.168.1.1, 1.1.1.1

[share]
drive_letter = Z:
unc_path = \\server\deployment
username = DOMAIN\deploy
password = replace-with-password

[auto_deploy]
enabled = false
wim_path = Z:\Images\Windows11.wim
image_index = 1
firmware = UEFI (GPT)
minimum_disk_size_gib = 100
maximum_disk_size_gib =
expected_disk_serial =
```

`gateway` and `dns` are optional. The adapter name must exactly match the name displayed in the **Network** tab. When both sections are present, startup first configures the adapter and then maps the SMB share. With only `[share]`, mapping is attempted immediately. The password is intentionally plaintext to support an offline WinPE USB workflow; protect that USB physically and restrict the SMB account to the minimum required permissions. Passwords are redacted from the application's command logs.

`[auto_deploy]` has one behavior: `enabled = true` starts a non-interactive deployment before the GUI is created, after drivers, Ethernet, and SMB initialization have completed. The WinPE command window displays progress and the same messages are written to `X:\Windows\Temp\WinPEImageDeployer-startup.log`. Invalid configuration, an inaccessible WIM, a missing image index, or any ambiguous disk layout prevents deployment and opens the GUI instead.

Auto-deploy is fail-closed. The physical disk containing `startup-config.ini` is protected, as is the disk containing a local WIM path. CD-ROM/ISO and SMB paths do not resolve to a cleanable disk. The size must meet `minimum_disk_size_gib`; optional `maximum_disk_size_gib` is an inclusive upper bound (positive integer, at least the minimum). Leave it empty or omit it for no upper bound. For example, `maximum_disk_size_gib = 1100` includes a 1 TB disk (~931 GiB) and excludes a 4 TB disk (~3725 GiB). `expected_disk_serial` is optional: when set, exactly one unprotected disk within the size bounds must have that exact serial, even if other disks are present; PowerShell disk metadata is required. When empty, target discovery uses DiskPart and exactly one unprotected disk within the size bounds must remain, so minimal WinPE does not need PowerShell. Zero or multiple matches skip deployment and open the GUI. Deployment runs DiskPart `clean` on the selected disk and destroys **all** its partitions; it cannot preserve another partition on that disk.

```powershell
powershell.exe -NoProfile -Command "Get-Disk | Format-Table Number,FriendlyName,BusType,SerialNumber,Size -AutoSize"
```

Alternatively, in the GUI, use **Deploy** or **Physical Disks** and copy the `S/N:` value shown for the target. Use the displayed serial text exactly as the `expected_disk_serial` value. If no serial number is available, leave the option empty and rely on the single-eligible-disk rule.

## Logs

The application displays current activity in the **Logs** tab. Select **Save log** to write a timestamped log file. Do not rely on `X:` for persistence because the WinPE RAM disk is discarded after reboot.

## Testing and coverage

The unit tests mock all operating-system tools, so they can run on a development computer without changing any disks:

```powershell
py -3.12 -m pip install -e .
py -3.12 -m unittest discover -s tests -v
py -3.12 -m coverage run -m unittest discover -s tests
py -3.12 -m coverage report
```

`coverage` is optional and is only needed for the second and third commands.

## Project layout

```text
src/winpe_deploy/
  command_runner.py  # Process execution and redacted logging
  services.py        # DISM, DiskPart, BCDBoot, and networking workflows
  gui.py             # Tkinter user interface
  models.py          # Typed value objects and validation
  main.py            # Application entry point
tests/               # Unit tests for command construction and validation
```

## Operational notes

- Capture only an offline Windows volume when using this tool in WinPE.
- Ensure WinPE has NIC and storage drivers for each target physical computer or virtual hardware platform before deployment. Inject drivers into the WinPE boot image when a target disk or network adapter is not visible.
- Ensure the deployed Windows image includes any boot-critical storage drivers required by the destination hardware.
- For Secure Boot deployments, use UEFI/GPT and a compatible Windows image. On physical computers, confirm Secure Boot and boot-mode settings in the firmware setup utility; in Hyper-V Generation 2, Secure Boot may be disabled temporarily only as a diagnostic step.
- The deployment workflow intentionally creates a standard, clean boot disk rather than attempting to preserve existing partitions or data.
- Test the full workflow on representative hardware before production rollout.