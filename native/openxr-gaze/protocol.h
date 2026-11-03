#pragma once
#include <stdint.h>
#include <stddef.h>

#define COVAS_GAZE_MAGIC 0x4358475a
#define COVAS_GAZE_VERSION 1
#define COVAS_GAZE_LAYER "XR_APILAYER_COVAS_NEXT_gaze"
#define COVAS_GAZE_MAPPING L"Local\\COVAS_NEXT_OpenXR_Gaze_v1_%lu"

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t size;
    volatile int32_t sequence;
    uint64_t timestamp_ms;
    uint32_t valid;
    uint32_t reference_space;
    float position[3];
    float orientation[4];
    uint32_t reserved;
} CovasGazePose;

_Static_assert(sizeof(CovasGazePose) == 64, "Gaze pose wire size changed");
_Static_assert(offsetof(CovasGazePose, timestamp_ms) == 16, "Gaze timestamp alignment changed");
