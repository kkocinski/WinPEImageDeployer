# winpe-media-content — content for boot media

Copy this entire `winpe-media-content` folder to the root of the WinPE boot media:

```text
<boot-media>:\winpe-media-content\
```

The final structure should be:

```text
<boot-media>:\winpe-media-content\
├── Drivers\
│   └── <vendor driver folders and .inf files>
└── startup-config.ini
```

## Drivers

Copy extracted driver packages into `Drivers`, including every file supplied with each `.inf` file (`.sys`, `.cat`, DLLs, and other companion files). Subfolders are supported, for example:

```text
Drivers\
├── Network\Intel-I219\*.inf
├── Storage\Intel-RST\*.inf
└── USB\*.inf
```

At WinPE startup, the application recursively finds and loads every `.inf` below `Drivers` with `drvload.exe`. When at least one driver is loaded, it waits 10 seconds before applying startup networking, so a newly installed Ethernet adapter has time to initialize.

## Startup configuration

1. Copy or rename `startup-config.ini.example` to `startup-config.ini`.
2. Edit the settings for Ethernet, optional SMB mapping, and optional auto-deploy. Ethernet and SMB mapping complete before automatic deployment or the GUI; SMB mapping retries up to 6 times at 5-second intervals.
3. Keep `[auto_deploy]` disabled until the target hardware layout has been validated.

The public repository includes only the example; any local active `startup-config.ini` is ignored by Git. It may contain a plaintext SMB password and may enable disk erasure. Review it before building boot media; never commit it or vendor drivers without permission.

With `enabled = true`, automatic deployment restarts WinPE after successful deployment and boot-file creation. Any validation failure opens the GUI instead.

## Text Editor

The **Text Editor** tab can open, edit, save, and save-as text files stored on local WinPE media. Use **Open startup-config.ini** when exactly one active startup configuration is present. Changes to `startup-config.ini` are saved immediately, but require a WinPE restart before pre-GUI drivers, Ethernet, and SMB startup actions use the new settings.

## Automatic deployment behavior

**Destructive scope:** The current workflow treats one physical target disk as dedicated to one Windows installation; it erases all existing partitions on that disk. "One disk, one partition" describes the intended single Windows/data volume, not the literal UEFI layout (which also needs EFI and MSR partitions). A separate D: data partition on the target disk will not be retained. Disk-to-disk copying and partition-to-partition copying/restoration are potential future features, not available in this version.

When `[auto_deploy] enabled = true`, WinPE validates the WIM and image index, protects the configuration-media disk and any local WIM disk, and deploys only if exactly one disk within the configured size bounds remains. Optional `maximum_disk_size_gib` is inclusive; leave it empty for no upper limit (e.g. `1100` permits a 1 TB disk but excludes a 4 TB disk). This runs before the GUI and writes status to both the WinPE command window and `X:\Windows\Temp\WinPEImageDeployer-startup.log`. Leave `expected_disk_serial` empty to use DiskPart-only discovery in minimal WinPE. When a serial is configured, PowerShell disk metadata is required to verify it. Invalid configuration or any ambiguity prevents deployment and opens the GUI.

## USB created through Rufus from an ISO

`<repo>` means the absolute path to your local clone. `<repo>\scripts\create-winpe-media.bat` copies this media-content folder into the WinPE `media` directory before it generates an ISO. Therefore, for Rufus media, first place your own drivers and `startup-config.ini` in this folder, then run:

```bat
<repo>\scripts\create-winpe-media.bat amd64 C:\WinPE-Image-Deployer-amd64 /ISO C:\WinPEImageDeployer.iso
```

The script copies it to:

```text
C:\WinPE-Image-Deployer-amd64\media\winpe-media-content\
```

Then generate the ISO and write it with Rufus. Do not assume that a later copy to a Rufus-written USB will be visible in every Rufus mode.