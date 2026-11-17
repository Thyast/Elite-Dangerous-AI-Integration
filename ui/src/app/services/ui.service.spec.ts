import { TestBed } from "@angular/core/testing";
import { Subject } from "rxjs";
import { BaseMessage, TauriService } from "./tauri.service";
import { UIService } from "./ui.service";

describe("shared desktop and VR navigation", () => {
    let output: Subject<BaseMessage>;
    let channel: jasmine.SpyObj<BroadcastChannel>;
    let service: UIService;

    beforeEach(() => {
        output = new Subject<BaseMessage>();
        channel = jasmine.createSpyObj<BroadcastChannel>("BroadcastChannel", ["postMessage", "close"]);
        spyOn(window, "BroadcastChannel").and.returnValue(channel);
        TestBed.configureTestingModule({
            providers: [{ provide: TauriService, useValue: { output$: output } }],
        });
        service = TestBed.inject(UIService);
    });

    function receive(data: object): void {
        channel.onmessage!(new MessageEvent("message", { data }));
    }

    it("requests the current selection on creation and broadcasts PC tab and subtab changes", () => {
        expect(channel.postMessage).toHaveBeenCalledWith({ type: "request-current" });
        service.showTab("plugin:ship:upgrades", "plan");
        expect(channel.postMessage).toHaveBeenCalledWith({
            type: "navigation", navigation: { type: "ui", show: "plugin:ship:upgrades", submenu: "plan" },
        });
    });

    it("applies another window's navigation without rebroadcasting it", () => {
        const navigation = { type: "ui", show: "navigation", submenu: "route" };
        const values: unknown[] = [];
        service.changeUI$.subscribe(value => values.push(value));
        channel.postMessage.calls.reset();
        receive({ type: "navigation", navigation });
        expect(values.at(-1)).toEqual(navigation);
        expect(channel.postMessage).not.toHaveBeenCalled();
    });

    it("delivers section requests once without replacing or replaying the current tab navigation", () => {
        const requests: unknown[] = [];
        service.sectionRequests$.subscribe(request => requests.push(request));
        output.next({type: "ui", timestamp: "", index: 0, show: "plugin:ship:upgrades", submenu: "session"});
        const request = {request_id: "request", expanded: false, index: 2};
        output.next({type: "ui", timestamp: "", index: 1, section: request});
        expect(requests).toEqual([request]);
        const lateRequests: unknown[] = [];
        service.sectionRequests$.subscribe(request => lateRequests.push(request));
        expect(lateRequests).toEqual([]);
        receive({type: "request-current"});
        expect(channel.postMessage).toHaveBeenCalledWith({
            type: "navigation", navigation: jasmine.objectContaining({show: "plugin:ship:upgrades", submenu: "session"}),
        });
    });

    it("keeps voice visibility separate from the selected tab and scrolling", () => {
        const navigation = { type: "ui", show: "plugin:ship:upgrades", submenu: "plan", timestamp: "", index: 0 };
        const visibility: boolean[] = [];
        service.overlayInterfaceVisible$.subscribe(value => visibility.push(value));
        output.next(navigation);
        output.next({ type: "ui", visible: false, timestamp: "", index: 1 });
        output.next({ type: "ui", scroll: "down", timestamp: "", index: 2 });
        channel.postMessage.calls.reset();
        receive({ type: "request-current" });
        expect(visibility).toEqual([true, false]);
        expect(channel.postMessage).toHaveBeenCalledWith({ type: "navigation", navigation });
        expect(channel.postMessage).toHaveBeenCalledWith({ type: "visibility", visible: false });
        output.next({ type: "ui", visible: true, timestamp: "", index: 3 });
        expect(visibility).toEqual([true, false, true]);
    });

    it("restores visibility from another window without changing tabs or rebroadcasting", () => {
        const values: boolean[] = [];
        service.overlayInterfaceVisible$.subscribe(value => values.push(value));
        channel.postMessage.calls.reset();
        receive({ type: "visibility", visible: false });
        expect(values).toEqual([true, false]);
        expect(channel.postMessage).not.toHaveBeenCalled();
    });

    it("keeps the selected view available for a new VR window even after voice scrolling", () => {
        const navigation = { type: "ui", show: "plugin:ship:upgrades", submenu: "plan", timestamp: "", index: 0 };
        output.next(navigation);
        output.next({ type: "ui", scroll: "down", timestamp: "", index: 1 });
        receive({ type: "request-current" });
        expect(channel.postMessage).toHaveBeenCalledWith({ type: "navigation", navigation });
    });

    it("does not replay a scroll command when there is no selected view", () => {
        output.next({ type: "ui", scroll: "down", timestamp: "", index: 0 });
        channel.postMessage.calls.reset();
        receive({ type: "request-current" });
        expect(channel.postMessage).toHaveBeenCalledOnceWith({ type: "visibility", visible: true });
    });

    it("closes its navigation channel when destroyed", () => {
        service.ngOnDestroy();
        expect(channel.close).toHaveBeenCalled();
    });

    it("temporarily reveals the GUI for adjustment and restores the previous hidden state", () => {
        const values: boolean[] = [];
        const adjusting: boolean[] = [];
        service.vrAdjustmentActive$.subscribe(value => adjusting.push(value));
        service.overlayInterfaceVisible$.subscribe(value => values.push(value));
        output.next({ type: "ui", visible: false, timestamp: "", index: 0 });
        output.next({ type: "vr_placement_state", active: true, timestamp: "", index: 1 });
        expect(values.at(-1)).toBeTrue();
        output.next({ type: "vr_placement_state", active: true, timestamp: "", index: 2 });
        output.next({ type: "ui", visible: false, timestamp: "", index: 3 });
        expect(values.at(-1)).toBeTrue();
        output.next({ type: "vr_placement_state", active: false, timestamp: "", index: 4 });
        expect(values.at(-1)).toBeFalse();
        expect(adjusting).toEqual([false, true, true, false]);
    });

    it("shares the original visibility with a VR window opened during adjustment", () => {
        output.next({ type: "ui", visible: false, timestamp: "", index: 0 });
        output.next({ type: "vr_placement_state", active: true, timestamp: "", index: 1 });
        channel.postMessage.calls.reset();
        receive({ type: "request-current" });
        expect(channel.postMessage).toHaveBeenCalledWith({ type: "adjustment", previousVisible: false });
        receive({ type: "adjustment", previousVisible: false });
        const adjusting: boolean[] = [];
        service.vrAdjustmentActive$.subscribe(value => adjusting.push(value));
        expect(adjusting).toEqual([true]);
        output.next({ type: "vr_placement_state", active: false, timestamp: "", index: 2 });
        expect(adjusting.at(-1)).toBeFalse();
        const values: boolean[] = [];
        service.overlayInterfaceVisible$.subscribe(value => values.push(value));
        expect(values).toEqual([false]);
    });
});
