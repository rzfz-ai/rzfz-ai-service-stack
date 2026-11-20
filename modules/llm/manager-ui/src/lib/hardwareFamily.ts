// #1518 (E5): one hardware-family rule for the console, mirroring the
// manager's `catalog.hardware_family()` / placement `_hardware_family()`.
//
// Workers register a SPECIFIC label (`amd-gfx1151`, `cuda`, and until a node
// re-registers also the legacy `cuda-gb10`), while a catalog entry lists a
// GENERIC one (`amd`, `nvidia`). Comparing the two literally greys out every
// card on such a fleet — a GB10-only box showed a catalog with nothing
// deployable at all. Normalize both sides before comparing.
export function hardwareFamily(value: string | null | undefined): string {
  const h = (value ?? "").toLowerCase();
  if (/amd|gfx|rocm|vulkan/.test(h)) return "amd";
  if (/nvidia|cuda/.test(h)) return "nvidia";
  if (/apple|metal|mlx/.test(h)) return "apple";
  if (h === "cpu" || h.endsWith("-cpu") || h.split("-").includes("cpu")) return "cpu";
  return h;
}

// Set of families a fleet of workers can serve. Empty = no workers known yet,
// in which case callers must NOT grey anything out (we cannot tell).
export function fleetFamilies(workers: ReadonlyArray<{ hardware?: string | null }> | undefined): Set<string> {
  return new Set((workers ?? []).map((w) => hardwareFamily(w.hardware)).filter(Boolean));
}

// True when at least one worker family matches one of the entry's families.
export function servableBy(fleet: Set<string>, entryHardware: readonly string[]): boolean {
  return fleet.size === 0 || entryHardware.some((h) => fleet.has(hardwareFamily(h)));
}
