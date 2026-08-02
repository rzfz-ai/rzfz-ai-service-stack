#!/bin/sh
# Dynamischer rocm-smi Mock für GPUStack v0.7.1 + Vulkan.
# Liest VRAM-Auslastung, GPU-Busy und Temperatur aus sysfs,
# damit GPUStack echte Werte anzeigt statt statischer 300MB.
#
# GPUStack ruft auf: rocm-smi --showid --showmeminfo vram ... --json
# und parst: "VRAM Total Memory (B)", "VRAM Total Used Memory (B)",
#            "GPU use (%)", "Temperature (Sensor edge) (C)"

# --- Dynamische Werte aus sysfs ---
CARD_DIR=""
for d in /sys/class/drm/card*/device; do
    if [ -f "$d/mem_info_vram_total" ]; then
        CARD_DIR="$d"
        break
    fi
done

if [ -n "$CARD_DIR" ]; then
    VRAM_TOTAL=$(cat "$CARD_DIR/mem_info_vram_total" 2>/dev/null || echo "103079215104")
    VRAM_USED=$(cat "$CARD_DIR/mem_info_vram_used" 2>/dev/null || echo "0")
    GPU_BUSY=$(cat "$CARD_DIR/gpu_busy_percent" 2>/dev/null || echo "0")

    # Temperatur: hwmon Sensor auslesen (in Milligrad, /1000 = °C)
    TEMP_RAW=""
    for hwmon in "$CARD_DIR"/hwmon/hwmon*/temp1_input; do
        if [ -f "$hwmon" ]; then
            TEMP_RAW=$(cat "$hwmon" 2>/dev/null)
            break
        fi
    done
    if [ -n "$TEMP_RAW" ]; then
        # Shell-Division: Milligrad -> Grad mit einer Dezimalstelle
        TEMP_INT=$((TEMP_RAW / 1000))
        TEMP_FRAC=$(( (TEMP_RAW % 1000) / 100 ))
        TEMP="${TEMP_INT}.${TEMP_FRAC}"
    else
        TEMP="0.0"
    fi

    # Power: hwmon Sensor (in Mikrowatt, /1000000 = W)
    POWER_RAW=""
    for pwmon in "$CARD_DIR"/hwmon/hwmon*/power1_average "$CARD_DIR"/hwmon/hwmon*/power1_input; do
        if [ -f "$pwmon" ]; then
            POWER_RAW=$(cat "$pwmon" 2>/dev/null)
            break
        fi
    done
    if [ -n "$POWER_RAW" ]; then
        POWER_INT=$((POWER_RAW / 1000000))
        POWER_FRAC=$(( (POWER_RAW % 1000000) / 1000 ))
        POWER="${POWER_INT}.${POWER_FRAC}"
    else
        POWER="0.0"
    fi
else
    # Fallback: statische Werte aus der originalen JSON
    VRAM_TOTAL="103079215104"
    VRAM_USED="0"
    GPU_BUSY="0"
    TEMP="0.0"
    POWER="0.0"
fi

# --- JSON-Ausgabe im Format das GPUStack erwartet ---
cat <<EOF
{"card0": {"Device Name": "AMD Radeon 8060S", "Device ID": "0x1586", "Device Rev": "0xd1", "Subsystem ID": "-0x72e3", "GUID": "36121", "Unique ID": "N/A", "Temperature (Sensor edge) (C)": "${TEMP}", "Current Socket Graphics Package Power (W)": "${POWER}", "GPU use (%)": "${GPU_BUSY}", "Serial Number": "N/A", "VRAM Total Memory (B)": "${VRAM_TOTAL}", "VRAM Total Used Memory (B)": "${VRAM_USED}", "Card Series": "AMD Radeon 8060S", "Card Model": "0x1586", "Card Vendor": "Advanced Micro Devices, Inc. [AMD/ATI]", "Card SKU": "STRXLGEN", "Node ID": "1", "GFX Version": "gfx1151"}}
EOF
