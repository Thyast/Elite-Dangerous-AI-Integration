#include <windows.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <openxr/openxr_loader_negotiation.h>
#include "protocol.h"

#ifdef NDEBUG
#error Native test assertions must remain enabled.
#endif

static int stage_supported = 1, pose_valid = 1, rotated, fail_session, fail_dispatch;
static int destroyed_instances, destroyed_sessions, destroyed_spaces, frames;
static uintptr_t next_instance = 1, next_session = 100;
static XrApiLayerNextInfo tail;
static XrTime located_at;

static XrResult XRAPI_CALL fake_create_instance(const XrInstanceCreateInfo *info, const XrApiLayerCreateInfo *layer, XrInstance *instance) {
    assert(info && layer->nextInfo == &tail);
    *instance = (XrInstance)next_instance++;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_destroy_instance(XrInstance instance) {
    assert(instance); destroyed_instances++; return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_create_session(XrInstance instance, const XrSessionCreateInfo *info, XrSession *session) {
    assert(instance && info && info->next == &tail);
    if (fail_session) return XR_ERROR_RUNTIME_FAILURE;
    *session = (XrSession)next_session++;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_destroy_session(XrSession session) {
    assert(session); destroyed_sessions++; return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_create_space(XrSession session, const XrReferenceSpaceCreateInfo *info, XrSpace *space) {
    assert(session && info->poseInReferenceSpace.orientation.w == 1);
    if (info->referenceSpaceType == XR_REFERENCE_SPACE_TYPE_STAGE && !stage_supported)
        return XR_ERROR_REFERENCE_SPACE_UNSUPPORTED;
    *space = (XrSpace)((uintptr_t)session * 16 + info->referenceSpaceType);
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_destroy_space(XrSpace space) {
    assert(space); destroyed_spaces++; return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_locate_space(XrSpace view, XrSpace world, XrTime time, XrSpaceLocation *location) {
    assert((uintptr_t)view % 16 == XR_REFERENCE_SPACE_TYPE_VIEW);
    assert((uintptr_t)world % 16 == (stage_supported ? XR_REFERENCE_SPACE_TYPE_STAGE : XR_REFERENCE_SPACE_TYPE_LOCAL));
    located_at = time;
    location->locationFlags = pose_valid ? XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT : 0;
    location->pose.position = (XrVector3f){1.25f, 1.4f, -2.5f};
    location->pose.orientation = rotated ? (XrQuaternionf){0, 0.70710678f, 0, 0.70710678f} : (XrQuaternionf){0, 0, 0, 1};
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_end_frame(XrSession session, const XrFrameEndInfo *info) {
    assert(session && info); frames++; return XR_FRAME_DISCARDED;
}
static void XRAPI_CALL fake_extension(void) {}
static XrResult XRAPI_CALL fake_get_proc(XrInstance instance, const char *name, PFN_xrVoidFunction *function) {
    assert(instance);
#define PROVIDE(xr_name, implementation) if (!strcmp(name, xr_name)) { *function = (PFN_xrVoidFunction)implementation; return XR_SUCCESS; }
    PROVIDE("xrDestroyInstance", fake_destroy_instance)
    PROVIDE("xrCreateSession", fake_create_session)
    PROVIDE("xrDestroySession", fake_destroy_session)
    PROVIDE("xrCreateReferenceSpace", fake_create_space)
    PROVIDE("xrDestroySpace", fake_destroy_space)
    PROVIDE("xrLocateSpace", fake_locate_space)
    if (!strcmp(name, "xrEndFrame") && fail_dispatch) return XR_ERROR_FUNCTION_UNSUPPORTED;
    PROVIDE("xrEndFrame", fake_end_frame)
    PROVIDE("xrTestExtension", fake_extension)
#undef PROVIDE
    return XR_ERROR_FUNCTION_UNSUPPORTED;
}

typedef struct {
    XrInstance instance;
    XrSession session;
    PFN_xrEndFrame end_frame;
    PFN_xrDestroySession destroy_session;
    PFN_xrDestroyInstance destroy_instance;
} Host;

static Host create_host(XrNegotiateApiLayerRequest *api) {
    XrApiLayerNextInfo next = {XR_LOADER_INTERFACE_STRUCT_API_LAYER_NEXT_INFO, XR_API_LAYER_NEXT_INFO_STRUCT_VERSION, sizeof(next)};
    next.nextGetInstanceProcAddr = fake_get_proc;
    next.nextCreateApiLayerInstance = fake_create_instance;
    next.next = &tail;
    XrApiLayerCreateInfo layer = {XR_LOADER_INTERFACE_STRUCT_API_LAYER_CREATE_INFO, XR_API_LAYER_CREATE_INFO_STRUCT_VERSION, sizeof(layer)};
    layer.nextInfo = &next;
    XrInstanceCreateInfo info = {XR_TYPE_INSTANCE_CREATE_INFO};
    Host host = {0};
    assert(api->createApiLayerInstance(&info, &layer, &host.instance) == XR_SUCCESS);
    PFN_xrCreateSession create_session;
    assert(api->getInstanceProcAddr(host.instance, "xrCreateSession", (PFN_xrVoidFunction *)&create_session) == XR_SUCCESS);
    XrSessionCreateInfo session = {XR_TYPE_SESSION_CREATE_INFO};
    session.next = &tail;
    if (fail_session) {
        assert(create_session(host.instance, &session, &host.session) == XR_ERROR_RUNTIME_FAILURE);
    } else {
        assert(create_session(host.instance, &session, &host.session) == XR_SUCCESS);
    }
    assert(api->getInstanceProcAddr(host.instance, "xrEndFrame", (PFN_xrVoidFunction *)&host.end_frame) == XR_SUCCESS);
    assert(api->getInstanceProcAddr(host.instance, "xrDestroySession", (PFN_xrVoidFunction *)&host.destroy_session) == XR_SUCCESS);
    assert(api->getInstanceProcAddr(host.instance, "xrDestroyInstance", (PFN_xrVoidFunction *)&host.destroy_instance) == XR_SUCCESS);
    PFN_xrVoidFunction extension;
    assert(api->getInstanceProcAddr(host.instance, "xrTestExtension", &extension) == XR_SUCCESS && extension == fake_extension);
    return host;
}

static void frame(Host *host) {
    XrFrameEndInfo info = {XR_TYPE_FRAME_END_INFO};
    info.displayTime = 123456;
    assert(host->end_frame(host->session, &info) == XR_FRAME_DISCARDED);
    assert(located_at == 123456);
}

int main(int argc, char **argv) {
    assert(argc >= 2);
    HMODULE library = LoadLibraryA(argv[1]);
    assert(library);
    PFN_xrNegotiateLoaderApiLayerInterface negotiate = (PFN_xrNegotiateLoaderApiLayerInterface)GetProcAddress(library, "xrNegotiateLoaderApiLayerInterface");
    assert(negotiate);
    XrNegotiateLoaderInfo loader = {XR_LOADER_INTERFACE_STRUCT_LOADER_INFO, XR_LOADER_INFO_STRUCT_VERSION, sizeof(loader)};
    loader.minInterfaceVersion = loader.maxInterfaceVersion = 1;
    loader.minApiVersion = XR_MAKE_VERSION(1, 0, 0);
    loader.maxApiVersion = XR_CURRENT_API_VERSION;
    XrNegotiateApiLayerRequest api = {XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST, XR_API_LAYER_INFO_STRUCT_VERSION, sizeof(api)};
    assert(negotiate(NULL, COVAS_GAZE_LAYER, &api) == XR_ERROR_INITIALIZATION_FAILED);
    assert(negotiate(&loader, "wrong-layer", &api) == XR_ERROR_INITIALIZATION_FAILED);
    assert(negotiate(&loader, COVAS_GAZE_LAYER, &api) == XR_SUCCESS);
    assert(api.layerInterfaceVersion == 1);
    assert(api.getInstanceProcAddr(NULL, NULL, NULL) == XR_ERROR_VALIDATION_FAILURE);
    Host host = create_host(&api);
    const int deferred = argc > 2 && !strcmp(argv[2], "--publish-later");
    if (!deferred) frame(&host);

    if (argc > 2 && (!strcmp(argv[2], "--publish") || deferred)) {
        printf("READY %lu\n", GetCurrentProcessId());
        fflush(stdout);
        char command[32];
        while (fgets(command, sizeof(command), stdin)) {
            if (!strncmp(command, "quit", 4)) break;
            pose_valid = strncmp(command, "invalid", 7) != 0;
            rotated = strncmp(command, "rotate", 6) == 0;
            Sleep(80);
            frame(&host);
            puts("UPDATED"); fflush(stdout);
        }
        assert(host.destroy_session(host.session) == XR_SUCCESS);
        assert(host.destroy_instance(host.instance) == XR_SUCCESS);
        FreeLibrary(library);
        return 0;
    }

    wchar_t name[96];
    swprintf(name, 96, COVAS_GAZE_MAPPING, GetCurrentProcessId());
    HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name);
    assert(mapping);
    const volatile CovasGazePose *pose = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(*pose));
    assert(pose && pose->magic == COVAS_GAZE_MAGIC && pose->version == 1 && pose->size == 64);
    assert(!(pose->sequence & 1) && pose->timestamp_ms && pose->valid && pose->reference_space == XR_REFERENCE_SPACE_TYPE_STAGE);
    assert(pose->position[0] == 1.25f && pose->orientation[3] == 1);
    const int32_t sequence = pose->sequence;
    frame(&host);
    assert(pose->sequence == sequence);
    pose_valid = 0; Sleep(80); frame(&host);
    assert(!pose->valid && pose->sequence > sequence);
    assert(host.destroy_session(host.session) == XR_SUCCESS);
    assert(!pose->valid && destroyed_sessions == 1 && destroyed_spaces == 2);
    UnmapViewOfFile((const void *)pose); CloseHandle(mapping);
    assert(host.destroy_instance(host.instance) == XR_SUCCESS && destroyed_instances == 1);

    stage_supported = 0; pose_valid = 1;
    host = create_host(&api); frame(&host);
    mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name);
    pose = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(*pose));
    assert(pose->valid && pose->reference_space == XR_REFERENCE_SPACE_TYPE_LOCAL);
    assert(host.destroy_instance(host.instance) == XR_SUCCESS && !pose->valid);
    assert(destroyed_spaces == 4);
    UnmapViewOfFile((const void *)pose); CloseHandle(mapping);

    fail_session = 1;
    host = create_host(&api);
    assert(host.destroy_instance(host.instance) == XR_SUCCESS);
    fail_session = 0;
    assert(frames == 4);
    FreeLibrary(library);
    puts("OpenXR layer tests passed: negotiation, chain forwarding, stage/local spaces, frame timing, tracking validity, throttling, lifecycle, failures.");
    return 0;
}
