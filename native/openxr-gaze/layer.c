#include <windows.h>
#include <string.h>
#include <wchar.h>
#include <openxr/openxr_loader_negotiation.h>
#include "protocol.h"

typedef struct InstanceState {
    XrInstance instance;
    PFN_xrGetInstanceProcAddr get_proc;
    PFN_xrDestroyInstance destroy_instance;
    PFN_xrCreateSession create_session;
    PFN_xrDestroySession destroy_session;
    PFN_xrCreateReferenceSpace create_space;
    PFN_xrDestroySpace destroy_space;
    PFN_xrLocateSpace locate_space;
    PFN_xrEndFrame end_frame;
    struct InstanceState *next;
} InstanceState;

typedef struct SessionState {
    XrSession session;
    InstanceState *instance;
    XrSpace view;
    XrSpace world;
    XrReferenceSpaceType reference_type;
    uint64_t last_sample_ms;
    struct SessionState *next;
} SessionState;

static SRWLOCK state_lock = SRWLOCK_INIT;
static InstanceState *instances;
static SessionState *sessions;
static HANDLE mapping;
static CovasGazePose *published;

static InstanceState *find_instance(XrInstance instance) {
    for (InstanceState *state = instances; state; state = state->next)
        if (state->instance == instance) return state;
    return NULL;
}

static SessionState *find_session(XrSession session) {
    for (SessionState *state = sessions; state; state = state->next)
        if (state->session == session) return state;
    return NULL;
}

static void close_publisher(void) {
    if (published) {
        InterlockedIncrement((volatile LONG *)&published->sequence);
        published->valid = 0;
        MemoryBarrier();
        InterlockedIncrement((volatile LONG *)&published->sequence);
        UnmapViewOfFile(published);
        published = NULL;
    }
    if (mapping) CloseHandle(mapping);
    mapping = NULL;
}

static void publish_pose(SessionState *state, XrTime display_time) {
    const uint64_t now = GetTickCount64();
    if (state->last_sample_ms && now - state->last_sample_ms < 50) return;
    state->last_sample_ms = now;
    if (!published) {
        wchar_t name[96];
        swprintf(name, 96, COVAS_GAZE_MAPPING, GetCurrentProcessId());
        mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(CovasGazePose), name);
        if (mapping) published = MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, sizeof(CovasGazePose));
        if (!published) {
            if (mapping) CloseHandle(mapping);
            mapping = NULL;
            OutputDebugStringA("COVAS:NEXT OpenXR gaze: cannot create pose mapping.\n");
            return;
        }
        ZeroMemory(published, sizeof(*published));
        published->magic = COVAS_GAZE_MAGIC;
        published->version = COVAS_GAZE_VERSION;
        published->size = sizeof(*published);
    }
    XrSpaceLocation location = {XR_TYPE_SPACE_LOCATION};
    XrResult result = XR_ERROR_POSE_INVALID;
    if (state->view && state->world)
        result = state->instance->locate_space(state->view, state->world, display_time, &location);
    const XrSpaceLocationFlags required = XR_SPACE_LOCATION_POSITION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_VALID_BIT;
    InterlockedIncrement((volatile LONG *)&published->sequence);
    published->timestamp_ms = now;
    published->reference_space = state->reference_type;
    published->valid = XR_SUCCEEDED(result) && (location.locationFlags & required) == required;
    published->position[0] = location.pose.position.x;
    published->position[1] = location.pose.position.y;
    published->position[2] = location.pose.position.z;
    published->orientation[0] = location.pose.orientation.x;
    published->orientation[1] = location.pose.orientation.y;
    published->orientation[2] = location.pose.orientation.z;
    published->orientation[3] = location.pose.orientation.w;
    MemoryBarrier();
    InterlockedIncrement((volatile LONG *)&published->sequence);
}

static void destroy_spaces(SessionState *state) {
    if (state->view && XR_FAILED(state->instance->destroy_space(state->view)))
        OutputDebugStringA("COVAS:NEXT OpenXR gaze: cannot destroy view space.\n");
    if (state->world && XR_FAILED(state->instance->destroy_space(state->world)))
        OutputDebugStringA("COVAS:NEXT OpenXR gaze: cannot destroy world space.\n");
}

static XrResult XRAPI_CALL layer_create_session(XrInstance instance, const XrSessionCreateInfo *info, XrSession *session) {
    AcquireSRWLockExclusive(&state_lock);
    InstanceState *parent = find_instance(instance);
    if (!parent) {
        ReleaseSRWLockExclusive(&state_lock);
        return XR_ERROR_HANDLE_INVALID;
    }
    SessionState *state = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*state));
    if (!state) {
        ReleaseSRWLockExclusive(&state_lock);
        return XR_ERROR_OUT_OF_MEMORY;
    }
    XrResult result = parent->create_session(instance, info, session);
    if (XR_FAILED(result)) {
        HeapFree(GetProcessHeap(), 0, state);
        ReleaseSRWLockExclusive(&state_lock);
        return result;
    }
    state->session = *session;
    state->instance = parent;
    XrReferenceSpaceCreateInfo space = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
    space.poseInReferenceSpace.orientation.w = 1;
    space.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_VIEW;
    if (XR_FAILED(parent->create_space(*session, &space, &state->view))) state->view = XR_NULL_HANDLE;
    space.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_STAGE;
    state->reference_type = XR_REFERENCE_SPACE_TYPE_STAGE;
    if (XR_FAILED(parent->create_space(*session, &space, &state->world))) {
        space.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
        state->reference_type = XR_REFERENCE_SPACE_TYPE_LOCAL;
        if (XR_FAILED(parent->create_space(*session, &space, &state->world))) state->world = XR_NULL_HANDLE;
    }
    if (!state->view || !state->world)
        OutputDebugStringA("COVAS:NEXT OpenXR gaze: reference spaces unavailable; tracking disabled for this session.\n");
    state->next = sessions;
    sessions = state;
    ReleaseSRWLockExclusive(&state_lock);
    return result;
}

static XrResult XRAPI_CALL layer_end_frame(XrSession session, const XrFrameEndInfo *info) {
    AcquireSRWLockExclusive(&state_lock);
    SessionState *state = find_session(session);
    if (!state) {
        ReleaseSRWLockExclusive(&state_lock);
        return XR_ERROR_HANDLE_INVALID;
    }
    PFN_xrEndFrame next = state->instance->end_frame;
    if (info) publish_pose(state, info->displayTime);
    ReleaseSRWLockExclusive(&state_lock);
    return next(session, info);
}

static XrResult XRAPI_CALL layer_destroy_session(XrSession session) {
    AcquireSRWLockExclusive(&state_lock);
    SessionState **link = &sessions;
    while (*link && (*link)->session != session) link = &(*link)->next;
    if (!*link) {
        ReleaseSRWLockExclusive(&state_lock);
        return XR_ERROR_HANDLE_INVALID;
    }
    SessionState *state = *link;
    PFN_xrDestroySession next = state->instance->destroy_session;
    destroy_spaces(state);
    *link = state->next;
    HeapFree(GetProcessHeap(), 0, state);
    if (!sessions) close_publisher();
    ReleaseSRWLockExclusive(&state_lock);
    return next(session);
}

static XrResult XRAPI_CALL layer_destroy_instance(XrInstance instance) {
    AcquireSRWLockExclusive(&state_lock);
    InstanceState **link = &instances;
    while (*link && (*link)->instance != instance) link = &(*link)->next;
    if (!*link) {
        ReleaseSRWLockExclusive(&state_lock);
        return XR_ERROR_HANDLE_INVALID;
    }
    InstanceState *state = *link;
    SessionState **child = &sessions;
    while (*child) {
        SessionState *session = *child;
        if (session->instance != state) { child = &session->next; continue; }
        destroy_spaces(session);
        *child = session->next;
        HeapFree(GetProcessHeap(), 0, session);
    }
    if (!sessions) close_publisher();
    PFN_xrDestroyInstance next = state->destroy_instance;
    *link = state->next;
    HeapFree(GetProcessHeap(), 0, state);
    ReleaseSRWLockExclusive(&state_lock);
    return next(instance);
}

static XrResult XRAPI_CALL layer_get_proc(XrInstance instance, const char *name, PFN_xrVoidFunction *function) {
    if (!name || !function) return XR_ERROR_VALIDATION_FAILURE;
    *function = NULL;
    if (!strcmp(name, "xrGetInstanceProcAddr")) {
        *function = (PFN_xrVoidFunction)layer_get_proc;
        return XR_SUCCESS;
    }
    AcquireSRWLockShared(&state_lock);
    InstanceState *state = find_instance(instance);
    PFN_xrGetInstanceProcAddr next = state ? state->get_proc : NULL;
    ReleaseSRWLockShared(&state_lock);
    if (!next) return XR_ERROR_HANDLE_INVALID;
#define HOOK(xr_name, implementation) if (!strcmp(name, xr_name)) { *function = (PFN_xrVoidFunction)implementation; return XR_SUCCESS; }
    HOOK("xrCreateSession", layer_create_session)
    HOOK("xrDestroySession", layer_destroy_session)
    HOOK("xrDestroyInstance", layer_destroy_instance)
    HOOK("xrEndFrame", layer_end_frame)
#undef HOOK
    return next(instance, name, function);
}

static XrResult XRAPI_CALL layer_create_instance(const XrInstanceCreateInfo *info, const XrApiLayerCreateInfo *layer, XrInstance *instance) {
    if (!info || !layer || !layer->nextInfo || !instance
        || layer->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_CREATE_INFO
        || layer->structVersion != XR_API_LAYER_CREATE_INFO_STRUCT_VERSION
        || layer->structSize != sizeof(*layer)
        || layer->nextInfo->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_NEXT_INFO
        || layer->nextInfo->structVersion != XR_API_LAYER_NEXT_INFO_STRUCT_VERSION
        || layer->nextInfo->structSize != sizeof(*layer->nextInfo)
        || !layer->nextInfo->nextGetInstanceProcAddr || !layer->nextInfo->nextCreateApiLayerInstance)
        return XR_ERROR_INITIALIZATION_FAILED;
    InstanceState *state = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*state));
    if (!state) return XR_ERROR_OUT_OF_MEMORY;
    state->get_proc = layer->nextInfo->nextGetInstanceProcAddr;
    XrApiLayerCreateInfo remaining = *layer;
    remaining.nextInfo = layer->nextInfo->next;
    XrResult result = layer->nextInfo->nextCreateApiLayerInstance(info, &remaining, instance);
    if (XR_FAILED(result)) { HeapFree(GetProcessHeap(), 0, state); return result; }
    state->instance = *instance;
#define LOAD(field, xr_name) if (XR_FAILED(state->get_proc(*instance, xr_name, (PFN_xrVoidFunction *)&state->field)) || !state->field) goto dispatch_failure
    LOAD(destroy_instance, "xrDestroyInstance");
    LOAD(create_session, "xrCreateSession");
    LOAD(destroy_session, "xrDestroySession");
    LOAD(create_space, "xrCreateReferenceSpace");
    LOAD(destroy_space, "xrDestroySpace");
    LOAD(locate_space, "xrLocateSpace");
    LOAD(end_frame, "xrEndFrame");
#undef LOAD
    AcquireSRWLockExclusive(&state_lock);
    state->next = instances;
    instances = state;
    ReleaseSRWLockExclusive(&state_lock);
    return result;
dispatch_failure:
    if (state->destroy_instance) state->destroy_instance(*instance);
    *instance = XR_NULL_HANDLE;
    HeapFree(GetProcessHeap(), 0, state);
    return XR_ERROR_INITIALIZATION_FAILED;
}

__declspec(dllexport) XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(
    const XrNegotiateLoaderInfo *loader, const char *name, XrNegotiateApiLayerRequest *request) {
    if (!loader || !request || !name || strcmp(name, COVAS_GAZE_LAYER)
        || loader->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO
        || loader->structVersion != XR_LOADER_INFO_STRUCT_VERSION || loader->structSize != sizeof(*loader)
        || request->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST
        || request->structVersion != XR_API_LAYER_INFO_STRUCT_VERSION || request->structSize != sizeof(*request)
        || loader->minInterfaceVersion > 1 || loader->maxInterfaceVersion < 1
        || loader->maxApiVersion < XR_MAKE_VERSION(1, 0, 0) || loader->minApiVersion > XR_CURRENT_API_VERSION)
        return XR_ERROR_INITIALIZATION_FAILED;
    request->layerInterfaceVersion = 1;
    request->layerApiVersion = loader->maxApiVersion < XR_CURRENT_API_VERSION ? loader->maxApiVersion : XR_CURRENT_API_VERSION;
    request->getInstanceProcAddr = layer_get_proc;
    request->createApiLayerInstance = layer_create_instance;
    return XR_SUCCESS;
}
