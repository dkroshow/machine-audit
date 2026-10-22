# Power and heat sources

Status: source receipts for the [Power view](../power-view.md) and
[Heat view](../heat-view.md), 2026-10-21. Documentation and code reading
only; no sensor, registry or sysfs read was made on any machine for this
note. Every finding here is unvalidated on the target devices unless marked
Measured. It chooses no schema, cadence or retention.

Labels: **Doc** is primary documentation or source at the linked revision;
**Code** is the source of an earlier private prototype; **Measured** is a
figure that prototype's own measurement notes report from its Intel test
Mac; **Unknown** is not established here. "Not found" means not found in the
sources listed at the end.

Revisions: Linux kernel `v6.8`. macOS headers were read in the macOS 15.5
SDK (build 24F74) shipped with Command Line Tools 16.4, on macOS 15.6.1
(24G90); the linked Apple open-source copies are later tags that contain
the same symbols (checked by search, not diffed). `man powermetrics` is the
page installed on macOS 15.6.1.

## macOS

| Source | Fields | Units and meaning | Access | Status |
| --- | --- | --- | --- | --- |
| AppleSMC user client, read commands only (Code) | Any 4-char `T` key; power keys `PSTR`, `PC0R`, `PG0R`, `PG2R`; fans `FNum`, `F%dAc`, `F%dMn`, `F%dMx` | Temperatures decoded from `sp78`, `flt`, `ui8`, `ui16`; fans from `fpe2`/`flt`; power keys read as watts, stored as milliwatt minute averages (Code); the prototype's labels: "Reported system power, CPU input rail, separate GPU rails", "Not wall or CPU-package watts; rails are unmapped" | No privilege; undocumented, "every failure means unknown" | Measured: 110 admitted sensors (CPU 9, GPU 8, storage 1, ambient 1, other 91); 7 headline keys + 4 power keys 1.53 ms CPU median; full sweep 12.96 ms; four fans 2.69 ms median, 13.90 ms worst. Apple silicon `flt` keys unverified |
| Admission rules (Code) | Absolute keys only; excludes `TCXR`/`TCXr`, `TC?E/F/J/T`, `TG?E/F`, `TH?F/R`; Intel raw/cooked duplicates kept once; plausible 0–120 °C | Known labels for `TCXc`, `TC0P`, `TG0D`, `TG1D`, `TH0a`, `TA0P`, `TPCD`, `TB0T`, `TC0H`, `TG0H`; others "Sensor N", grouped by prefix rule | — | Working rules, not a sensor map |
| Fan cadence (Code) | 5 s; after 12 reads, 60 s if the median exceeds 5 ms | Level quiet < 25 %, audible < 70 %, loud, of the fan's own range | — | — |
| Thermal pressure (Doc: [`OSThermalNotification.h`][libc-thermal]; Code) | `kOSThermalNotificationPressureLevelName` notify state | `TARGET_OS_OSX` branch: 0 nominal, 1 moderate, 2 heavy, 3 trapping, 4 sleeping; other branch 0, 10, 20, 30, 40, 50. No timestamp: the prototype records receipt time | No privilege; event-driven | — |
| `thermalState` (Doc: [Apple][apple-thermal]; SDK `NSProcessInfo.h`) | nominal, fair, serious, critical | Header: fair "fans may become audible"; serious "fans at maximum speed, performance may be impacted"; "unknown or unsupported" returns nominal | No privilege | Not used by the prototype |
| `IOPMCopyCPUPowerStatus` (Doc: [`IOPMLib.h`][ioku-pmlib], [`IOPM.h`][xnu-iopm]) | `CPU_Speed_Limit` (0–100 % of maximum speed), `CPU_Available_CPUs`, `CPU_Scheduler_Limit` (% of CPU time); `Graphics_Power_Performance`; change notify `com.apple.system.power.CPU`; `IOPMGetThermalWarningLevel` Normal/Danger/Crisis | `kIOReturnNotFound` when not published | No privilege | Code reads the speed limit at 5 s, Intel only ("Not reported on Apple silicon"); other keys, the notify key and the warning level unverified |
| Power sources (Doc: [`IOPSKeys.h`][ioku-pskeys], [`IOPowerSources.h`][ioku-ps]; Code) | Providing source (AC, battery, UPS); `Type`, `Current Capacity`, `Max Capacity`, `DesignCapacity`, `Is Charging`, `Is Charged`, `Time to Empty`, `Time to Full Charge`, `Voltage`, `Current`, `Temperature`, `BatteryHealth`, `BatteryHealthCondition`, `BatteryFailureModes`; adapter keys incl. `Watts` | Apple batteries publish capacities in percent; voltage mV, current mA (sign not stated), temperature °C; times in minutes, `-1` means "Still Calculating the Time"; notify keys for source change, time remaining, low battery carry no timestamp | No privilege | Code stores charge, source, charging; battery watts null; UPS is not the Mac's battery. Cycle count not found in the header. Adapter-detail call not found in the header read |
| `powermetrics` via root helper (Doc: `man powermetrics`; Code) | plist stream, samplers `cpu_power,thermal` (ARM adds `gpu_power`), 30 s interval; parsed `package_watts`, `freq_hz` | Manual: "Average power values reported by powermetrics are estimated and may be inaccurate ... should not be used for any comparison between devices"; `--show-process-energy` "a rough proxy ... weighting ... platform specific"; `--show-plimits` "only available on certain hardware" | Root (helper) | Keys verified on an Intel capture; ARM GPU keys unverified. Measured (derived): child about 0.075 % of a core at 30 s plus parse |
| IOReport Energy Model group (unverified outside report) | CPU, GPU, ANE, DRAM energy counters | Reported as unprivileged on Apple silicon | Private library | Unvalidated; candidate only |
| `isLowPowerModeEnabled` (Doc: [Apple][apple-lpm]; SDK `NSProcessInfo.h`) | Boolean, macOS 12+ | Returns NO where unsupported | No privilege | Unverified in use |

## Ubuntu

Documentation only, kernel `v6.8`. No Ubuntu machine, kernel build or
permission set has been checked; modes are what the kernel sets, and a
distribution may restrict further.

| Source | Fields | Units and meaning | Access | Status |
| --- | --- | --- | --- | --- |
| Thermal zones (Doc: [class ABI][k-class-thermal], [sysfs-api][k-thermal-api]) | `type`, `temp`, `mode`, `policy`, `trip_point_Y_temp`, `trip_point_Y_type`, `trip_point_Y_hyst`, `cdevY`, `cdevY_trip_point`, `cdevY_weight` | `temp`, trip temps millidegree Celsius, RO; types `critical`, `hot`, `passive`, `active[0-*]` (ACPI); `hyst` RW, Celsius; no event timestamp in sysfs | World-readable | Zone set per machine Unknown |
| Cooling devices (Doc: same) | `type` (`Fan`, `Processor`, `LCD` for ACPI), `max_state`, `cur_state`, `stats/time_in_state_ms`, `stats/total_trans`, `stats/trans_table`, `stats/reset` | `cur_state` 0 = no cooling, `max_state` = maximum; RW, Required; `time_in_state_ms` kernel-kept per state, RO; `reset` WO | Never written by this product | — |
| hwmon (Doc: [sysfs-interface][k-hwmon], [class ABI][k-class-hwmon]) | `name`, `label`, `update_interval`; `tempN_input/_label/_max/_crit/_emergency`; `fanN_input/_min/_max/_label`; `pwmN`; `powerN_input`, `powerN_average` + `_average_interval`, `powerN_cap`, `powerN_max`, `powerN_crit`; `energyN_input` | `tempN_input` millidegree Celsius RO; `fanN_input` RPM RO; `powerN_input`, `_average` microwatt RO; `energyN_input` microjoule RO cumulative; `tempN_crit` RW; a disabled sensor reads `-ENODATA` | Readable; caps and thresholds RW for root, never written | "There is no standard that ensures ... the second temperature sensor is connected to the CPU"; amdgpu `power1_*` includes the CPU on APUs; amdgpu hwmon reads wake a suspended GPU at v6.8 ([memory-gpu sources](memory-gpu-sources.md)) |
| Intel `thermal_throttle` (Doc: [`therm_throt.c`][k-throt]) | Per CPU `core_throttle_count`, `core_throttle_max_time_ms`, `core_throttle_total_time_ms`; `package_*` when `X86_FEATURE_PTS`; `core_power_limit_count`, `package_power_limit_count` only when `int_pln_enable` (default off) | Count of threshold-high interrupts; max time in one throttled episode; cumulative throttled time, ms, kernel-kept | Mode 0444 | Intel only; AMD not covered here |
| cpufreq (Doc: [cpufreq.rst][k-cpufreq], [intel_pstate.rst][k-pstate], [cpu ABI][k-cpu-abi], [`cpufreq.c`][k-cpufreq-c]) | `scaling_cur_freq`, `cpuinfo_cur_freq`, `cpuinfo_max_freq`, `scaling_max_freq`, `scaling_driver`, `scaling_governor`, `cpufreq/boost` | kHz. `scaling_cur_freq` "may or may not reflect the frequency the CPU is actually running at"; x86 may estimate more precisely. `cpuinfo_cur_freq` "should not be present" when undeterminable | `cpuinfo_cur_freq` mode 0400 (root); others readable | intel_pstate with HWP lets the processor pick P-states, reduced "in a thermal stress or a power limit violation situation". `throttle_stats` under cpufreq is POWERNV only |
| Intel RAPL via powercap (Doc: [powercap.rst][k-powercap], [`powercap_sys.c`][k-powercap-c], [`intel_rapl_common.c`][k-rapl-c]) | Zones `intel-rapl:N` named `package-N`; subzones `core`, `uncore`, `dram`, `psys` where present; `energy_uj`, `max_energy_range_uj`, `constraint_X_power_limit_uw`, `constraint_X_time_window_us`, `constraint_X_name`, `constraint_X_max_power_uw`, `enabled` | Microjoules, cumulative, wraps at the stated range; "there is no power_uw attribute" for RAPL; constraint limit in microwatts over its window. The RAPL measurement callbacks at v6.8 provide `get_energy_uj` and `get_max_energy_range_uj`: no reset callback, so `energy_uj` is read-only here and cannot be zeroed through sysfs, and no `max_power_range_uw`, since the framework creates those attributes only when a driver supplies the callback. `constraint_X_max_power_uw` comes from the package power-info field (PL1 maps to `THERMAL_SPEC_POWER`, PL2 to `MAX_POWER`) and PL4 is doubled by a stated heuristic; it is a reported limit capability, not a demonstrated instantaneous bound. Reinitialisation across suspend or reboot is Unknown | `energy_uj` mode `S_IRUSR` (root); constraints `S_IRUGO`, limit and window `S_IWUSR` | `psys` scope platform-defined; no whole-machine claim in the doc; counter continuity to validate on the target |
| `power_supply` (Doc: [class ABI][k-class-power], [class doc][k-psy-doc]) | `type`, `present`, `status`, `capacity`, `capacity_level`, `charge_now/_full/_full_design`, `energy_now/_full/_full_design`, `voltage_now`, `current_now`, `power_now`, `temp`, `cycle_count`, `health`, `time_to_empty_now`, `time_to_full_now`, `online`, `technology`, `charge_control_*_threshold` | µV, µA, µAh, µWh, seconds, tenths of °C; `charge_*` and `energy_*` are different families ("Don't mix them"); `current_now` "Negative values are used for discharging batteries"; `power_now` sign not stated; `cycle_count` "Integer = 0: cycle_count info is not available"; `status` Unknown, Charging, Discharging, Not charging, Full | Readable; `status`, `online`, thresholds RW for some drivers, never written | Which family a laptop exposes is Unknown |

## Cautions carried into the views

- None of the routine sources surveyed gives per-process energy;
  `powermetrics`'s process energy number is a weighted proxy.
- Rails, packages, subzones and device power are not a partition. A
  system-named source (SMC "system", `psys`) is shown under its own label
  and documentation status, not as machine or wall consumption; battery
  discharge is net battery flow.
- Temperatures carry no limit on macOS; on Ubuntu, limits are published
  trip points and thresholds, not measured consequences.
- Throttling evidence is a speed limit, a throttle counter, a cooling state
  or a driver's clock-event reason; a thermal level is an OS judgement; a
  frequency or a crossing corroborates; a power cap is a Power fact. The
  [GPU view](../gpu-view.md) treats power and thermal limits as capacity
  context; NVIDIA clock-event reason names are in
  [memory-gpu sources](memory-gpu-sources.md#capacity-1).
- No surveyed macOS notify key or Linux sysfs file carries an event
  timestamp; only kernel-kept durations (`time_in_state_ms`,
  `*_throttle_total_time_ms`) are source-measured time.
- Energy counter deltas need source-specific continuity and wrap rules
  validated on the target; ambiguous decreases or gaps stay unknown, and no
  generic attribute establishes a safe interval by its name alone.
- Reads with side effects: AMD `PerformanceStatistics` on Big Mac and amdgpu
  sysfs at v6.8 ([GPU view](../gpu-view.md#read-side-effects)).

## Sources

Code citations refer to an earlier private prototype and are not linked.

Apple: [`IOPSKeys.h`][ioku-pskeys], [`IOPowerSources.h`][ioku-ps],
[`IOPMLib.h`][ioku-pmlib] (IOKitUser-100231.120.3); [`IOPM.h`][xnu-iopm]
(xnu-12377.121.6); [`OSThermalNotification.h`][libc-thermal]
(Libc-1752.120.2); [ProcessInfo.ThermalState][apple-thermal];
[isLowPowerModeEnabled][apple-lpm]; `man powermetrics` on macOS 15.6.1.

Linux `v6.8`: [hwmon sysfs-interface][k-hwmon], [sysfs-class-hwmon][k-class-hwmon],
[thermal sysfs-api][k-thermal-api], [sysfs-class-thermal][k-class-thermal],
[sysfs-class-power][k-class-power], [power_supply_class][k-psy-doc],
[powercap][k-powercap], [sysfs-devices-system-cpu][k-cpu-abi],
[cpufreq][k-cpufreq], [intel_pstate][k-pstate], [`therm_throt.c`][k-throt],
[`powercap_sys.c`][k-powercap-c], [`intel_rapl_common.c`][k-rapl-c],
[`cpufreq.c`][k-cpufreq-c].

[ioku-pskeys]: https://github.com/apple-oss-distributions/IOKitUser/blob/IOKitUser-100231.120.3/ps.subproj/IOPSKeys.h
[ioku-ps]: https://github.com/apple-oss-distributions/IOKitUser/blob/IOKitUser-100231.120.3/ps.subproj/IOPowerSources.h
[ioku-pmlib]: https://github.com/apple-oss-distributions/IOKitUser/blob/IOKitUser-100231.120.3/pwr_mgt.subproj/IOPMLib.h
[xnu-iopm]: https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.121.6/iokit/IOKit/pwr_mgt/IOPM.h
[libc-thermal]: https://github.com/apple-oss-distributions/Libc/blob/Libc-1752.120.2/include/libkern/OSThermalNotification.h
[apple-thermal]: https://developer.apple.com/documentation/foundation/processinfo/thermalstate-swift.enum
[apple-lpm]: https://developer.apple.com/documentation/foundation/processinfo/islowpowermodeenabled
[k-hwmon]: https://github.com/torvalds/linux/blob/v6.8/Documentation/hwmon/sysfs-interface.rst
[k-class-hwmon]: https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/testing/sysfs-class-hwmon
[k-thermal-api]: https://github.com/torvalds/linux/blob/v6.8/Documentation/driver-api/thermal/sysfs-api.rst
[k-class-thermal]: https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/testing/sysfs-class-thermal
[k-class-power]: https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/testing/sysfs-class-power
[k-psy-doc]: https://github.com/torvalds/linux/blob/v6.8/Documentation/power/power_supply_class.rst
[k-powercap]: https://github.com/torvalds/linux/blob/v6.8/Documentation/power/powercap/powercap.rst
[k-cpu-abi]: https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/testing/sysfs-devices-system-cpu
[k-cpufreq]: https://github.com/torvalds/linux/blob/v6.8/Documentation/admin-guide/pm/cpufreq.rst
[k-pstate]: https://github.com/torvalds/linux/blob/v6.8/Documentation/admin-guide/pm/intel_pstate.rst
[k-throt]: https://github.com/torvalds/linux/blob/v6.8/drivers/thermal/intel/therm_throt.c
[k-powercap-c]: https://github.com/torvalds/linux/blob/v6.8/drivers/powercap/powercap_sys.c
[k-rapl-c]: https://github.com/torvalds/linux/blob/v6.8/drivers/powercap/intel_rapl_common.c
[k-cpufreq-c]: https://github.com/torvalds/linux/blob/v6.8/drivers/cpufreq/cpufreq.c
