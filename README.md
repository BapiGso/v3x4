# v3x4

Intel(R) Xeon(R) Processor Max Effort Turbo Boost UEFI DXE driver.

This fork keeps the original DXE driver flow and MSR/OC Mailbox programming model, while making the target CPU list and build path easier to maintain.

## Supported CPUIDs

Default CPUID whitelist:

- `0x306F0` - Haswell-EP/EX v3 early ES
- `0x306F1` - Haswell-EP/EX v3 ES/QS
- `0x306F2` - Haswell-EP/EX v3
- `0x306F3` - Haswell-EP/EX v3
- `0x306F4` - Haswell-EP/EX v3

Experimental Broadwell-EP/EX v4 entry:

- `0x406F1` - Xeon E5/E7 v4 / Broadwell-EP/EX, guarded by `ENABLE_BROADWELL_EP_EXPERIMENTAL`

Broadwell/v4 support is experimental only. The v4 OC Mailbox path may be more useful for voltage experiments/undervolting than for all-core turbo ratio unlocking, and this project does not guarantee that the turbo ratio hack works on Xeon v4.

To bypass CPUID validation for local experiments, add `CPUID_BYPASS_CHECK` / `0xFFFFFFFF` to `BUILD_TARGET_CPUID_WHITELIST` in `v3x4.c`. Bypass only skips the CPUID whitelist check; microcode and OC Lock checks still apply.

## Requirements

- CPU microcode update revision must be `0x00000000` at driver execution time. If BIOS/firmware loads a microcode patch during POST, the driver aborts.
- OC Lock / Overclock Enable lock bit in `MSR_FLEX_RATIO` must be clear. If it is set, the driver aborts.
- A compatible UEFI environment with MP Services available is required. `v3x4.inf` depends on `gEfiMpServiceProtocolGuid` so the DXE driver does not load before MP Services.
- Use at your own risk. Wrong voltage, ratio, or firmware settings can make a system unstable or unbootable until the driver is removed or firmware settings are recovered.

## Default Voltage Settings

By default, the driver applies a conservative IA Core undervolt and keeps other voltage behavior unchanged:

- `IACORE_ADAPTIVE_OFFSET[] = _FVID_MINUS_50_MV` (`-50mV`)
- `CLR_ADAPTIVE_OFFSET[] = _DEFAULT_FVID` (`0x0`)
- `SA_ADAPTIVE_OFFSET[] = _DEFAULT_FVID` (`0x0`)
- `SVID_FIXED_VCCIN[] = _DYNAMIC_SVID` (`0x0`)
- `CPU_SET_FIXED_VCCIN = FALSE`

In other words, default builds apply only the IA Core `-50mV` adaptive offset unless these constants are edited before compiling.

## Runtime Logs

During package validation, the driver prints:

- Current CPUID
- Full target CPUID whitelist
- Full 32-bit microcode revision from `MSR_IA32_BIOS_SIGN_ID`
- OC Lock state and raw `MSR_FLEX_RATIO` value
- Experimental warning when the Broadwell/v4 entry is enabled and matched

## GitHub Actions Release Build

The workflow in `.github/workflows/build.yml` builds on Ubuntu with modern edk2. It uses the modern `GCC` toolchain tag, and falls back to `GCC5` only if an older edk2 checkout still defines it. It is intended to run when a version tag is pushed:

- Target: `RELEASE`
- Architecture: `X64`
- Main artifact: `v3x4.efi`
- Optional artifact: `v3x4.ffs` generated with `GenSec` + `GenFfs` when available
- Release upload: artifacts are attached to the GitHub Release for the pushed tag

To create a release build:

```sh
git tag v1.1.0
git push origin v1.1.0
```

After the workflow finishes, open the GitHub release for that tag and download `v3x4.efi` and, when generation succeeds, `v3x4.ffs`. The workflow also keeps a normal Actions artifact named `v3x4-X64-RELEASE-GCC` for debugging/manual downloads.

## Local edk2 Build

One simple local layout is to copy this module into `MdeModulePkg/v3x4` inside an edk2 checkout, add `MdeModulePkg/v3x4/v3x4.inf` to `MdeModulePkg/MdeModulePkg.dsc`, then run:

```sh
make -C BaseTools
. ./edksetup.sh
build -p MdeModulePkg/MdeModulePkg.dsc -m MdeModulePkg/v3x4/v3x4.inf -a X64 -b RELEASE -t GCC
```

The `.efi` output is typically under:

```text
Build/MdeModule/RELEASE_GCC/X64/MdeModulePkg/v3x4/v3x4/OUTPUT/v3x4.efi
```

## Legacy Usage Notes

The original usage model still applies: load `v3x4.efi` from an EFI shell or add it as a boot-time UEFI driver, for example:

```text
bcfg driver add 0 fs1:\EFI\Boot\v3x4.efi "V3 Full Turbo"
```

Keep a recovery path available before experimenting, such as temporarily removing the EFI binary from the boot partition.
