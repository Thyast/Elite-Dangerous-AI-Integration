import { fakeAsync, TestBed, tick } from "@angular/core/testing";
import { Subject } from "rxjs";
import { ChatMessage, ChatService, DisplayChatMessage } from "./chat.service";
import { BaseMessage, TauriService } from "./tauri.service";
import { UIService } from "./ui.service";

describe("ChatService event display grouping", () => {
    const enabledKey = "covas.ui.chat-event-grouping.enabled";
    const delayKey = "covas.ui.chat-event-grouping.delay";
    let storedSettings: Map<string, string | null>;
    let output: Subject<BaseMessage>;
    let service: ChatService;
    let displayed: DisplayChatMessage[];
    let received: ChatMessage[];
    let index: number;

    beforeEach(() => {
        storedSettings = new Map([enabledKey, delayKey].map((key) => [key, localStorage.getItem(key)]));
        localStorage.removeItem(enabledKey);
        localStorage.removeItem(delayKey);
        output = new Subject<BaseMessage>();
        displayed = [];
        received = [];
        index = 0;
        TestBed.configureTestingModule({
            providers: [
                ChatService,
                { provide: TauriService, useValue: { output$: output.asObservable() } },
                { provide: UIService, useValue: { showTab: jasmine.createSpy("showTab") } },
            ],
        });
        service = TestBed.inject(ChatService);
        observeService();
    });

    afterEach(() => {
        service.ngOnDestroy();
        output.complete();
        for (const [key, value] of storedSettings) {
            if (value === null) {
                localStorage.removeItem(key);
            } else {
                localStorage.setItem(key, value);
            }
        }
    });

    function observeService(): void {
        service.displayChatHistory$.subscribe((messages) => displayed = messages);
        service.chatMessage$.subscribe((message) => {
            if (message) {
                received.push(message);
            }
        });
    }

    function send(message = "FleetCarrierDiscovered", role = "event"): ChatMessage {
        const chatMessage: ChatMessage = {
            type: "chat",
            timestamp: new Date().toISOString(),
            index: ++index,
            role,
            message,
        };
        output.next(chatMessage);
        return chatMessage;
    }

    function restartService(): void {
        service.ngOnDestroy();
        service = new ChatService(TestBed.inject(TauriService), TestBed.inject(UIService));
        observeService();
    }

    it("preserves immediate display by default", () => {
        expect(service.eventGrouping).toEqual({ enabled: false, delaySeconds: 2 });
        const first = send();
        const second = send();
        expect(displayed).toEqual([first, second]);
        expect(displayed[0]).toBe(first);
    });

    it("delays only display while delivering all original events immediately", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        const first = send();
        const second = send();
        expect(received).toEqual([first, second]);
        expect(service.getCurrentChat()).toEqual([first, second]);
        expect(displayed).toEqual([]);
        tick(1999);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed).toEqual([{ ...first, repeatCount: 2 }]);
        expect(first["repeatCount"]).toBeUndefined();
        expect(second["repeatCount"]).toBeUndefined();
    }));

    it("restarts the inactivity timer on each occurrence", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        tick(1500);
        send();
        tick(1999);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed.length).toBe(1);
        expect(displayed[0].repeatCount).toBe(2);
    }));

    it("uses independent timers for interleaved event types", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        tick(500);
        const station = send("StationDiscovered");
        tick(500);
        send();
        tick(1499);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed).toEqual([{ ...station, repeatCount: 1 }]);
        tick(499);
        expect(displayed.length).toBe(1);
        tick(1);
        expect(displayed.map((message) => [message.message, message.repeatCount])).toEqual([
            ["StationDiscovered", 1],
            ["FleetCarrierDiscovered", 2],
        ]);
    }));

    it("forces publication exactly five seconds after the first event during continuous traffic", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        const first = send();
        for (let occurrence = 1; occurrence < 10; occurrence++) {
            tick(500);
            send();
        }
        tick(499);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed).toEqual([{ ...first, repeatCount: 10 }]);
        send();
        tick(2000);
        expect(displayed.map((message) => message.repeatCount)).toEqual([10, 1]);
    }));

    it("counts a system discovery flood with many interleaved event types", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        const waves = [
            [
                ...Array<string>(6).fill("FleetCarrierDiscovered"),
                "StationDiscovered",
                "FleetCarrierDiscovered", "FleetCarrierDiscovered",
            ],
            [
                ...Array<string>(7).fill("FleetCarrierDiscovered"),
                "NavBeaconDiscovered", "FleetCarrierDiscovered",
                ...Array<string>(4).fill("CombatDiscovered"),
                "InstallationDiscovered", "GenericDiscovered", "FleetCarrierDiscovered",
            ],
            [
                "GenericDiscovered", "InstallationDiscovered",
                "FleetCarrierDiscovered", "FleetCarrierDiscovered", "GenericDiscovered",
                "FleetCarrierDiscovered", "StationDiscovered",
                "FleetCarrierDiscovered", "FleetCarrierDiscovered",
                "InstallationDiscovered", "GenericDiscovered", "MegashipDiscovered",
                "FleetCarrierDiscovered", "StationDiscovered",
                "FleetCarrierDiscovered", "FleetCarrierDiscovered",
                "OutpostDiscovered", "OutpostDiscovered", "FleetCarrierDiscovered",
            ],
        ];
        for (const wave of waves) {
            wave.forEach((name) => send(name));
            tick(1000);
        }
        expect(displayed.some((message) => message.message === "FleetCarrierDiscovered")).toBeFalse();
        tick(999);
        expect(displayed.some((message) => message.message === "FleetCarrierDiscovered")).toBeFalse();
        tick(1);
        const totals: Record<string, number> = {};
        for (const message of displayed) {
            totals[message.message] = (totals[message.message] ?? 0) + (message.repeatCount ?? 1);
        }
        expect(totals).toEqual({
            FleetCarrierDiscovered: 26,
            StationDiscovered: 3,
            NavBeaconDiscovered: 1,
            CombatDiscovered: 4,
            InstallationDiscovered: 3,
            GenericDiscovered: 4,
            MegashipDiscovered: 1,
            OutpostDiscovered: 2,
        });
        expect(displayed.filter((message) => message.message === "StationDiscovered")
            .map((message) => message.repeatCount)).toEqual([1, 2]);
        expect(displayed.length).toBe(9);
        expect(received.length).toBe(44);
    }));

    it("keeps forcing publication at five-second intervals during a sustained flood", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        for (let occurrence = 0; occurrence < 30; occurrence++) {
            send();
            tick(500);
        }
        expect(displayed.map((message) => message.repeatCount)).toEqual([10, 10, 10]);
        expect(service.getCurrentChat().length).toBe(30);
    }));

    it("honors the configured inactivity delay", fakeAsync(() => {
        service.setEventGrouping(true, 1);
        send();
        tick(999);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed.length).toBe(1);
    }));

    it("caps a five-second inactivity delay from the start of the batch", fakeAsync(() => {
        service.setEventGrouping(true, 5);
        send();
        tick(4500);
        send();
        tick(499);
        expect(displayed).toEqual([]);
        tick(1);
        expect(displayed[0].repeatCount).toBe(2);
    }));

    it("does not delay or group messages, replies, actions, or warnings", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        const messages = ["cmdr", "covas", "action", "warn", "error", "quest", "npc_message", "plugin"]
            .flatMap((role) => [send("same text", role), send("same text", role)]);
        expect(displayed).toEqual(messages);
        tick(2000);
        expect(displayed.length).toBe(messages.length + 1);
        expect(displayed.at(-1)?.repeatCount).toBe(1);
    }));

    it("groups event names case-insensitively without changing the displayed name", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        const first = send("FleetCarrierDiscovered");
        send(" fleetcarrierdiscovered ", "EVENT");
        tick(2000);
        expect(displayed).toEqual([{ ...first, repeatCount: 2 }]);
    }));

    it("starts a separate batch after an inactivity flush", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        send();
        tick(2000);
        send();
        tick(1999);
        expect(displayed.map((message) => message.repeatCount)).toEqual([2]);
        tick(1);
        expect(displayed.map((message) => message.repeatCount)).toEqual([2, 1]);
        expect(new Set(displayed.map((message) => message.index)).size).toBe(2);
    }));

    it("flushes pending batches when grouping is disabled without rewriting history", fakeAsync(() => {
        const ungrouped = send();
        service.setEventGrouping(true, 2);
        send();
        send();
        service.setEventGrouping(false, 2);
        expect(displayed[0]).toBe(ungrouped);
        expect(displayed[1].repeatCount).toBe(2);
        const immediate = send();
        expect(displayed.at(-1)).toBe(immediate);
        tick(5000);
        expect(displayed.length).toBe(3);
    }));

    it("flushes pending batches on a delay change and applies the new delay", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        tick(500);
        service.setEventGrouping(true, 1);
        expect(displayed.length).toBe(1);
        send();
        tick(999);
        expect(displayed.length).toBe(1);
        tick(1);
        expect(displayed.length).toBe(2);
    }));

    it("does not flush a batch when unchanged settings are submitted", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        service.setEventGrouping(true, 2);
        expect(displayed).toEqual([]);
        tick(2000);
        expect(displayed.length).toBe(1);
    }));

    it("cancels pending batches on clear without losing the grouping preference", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        tick(1000);
        service.clearChat();
        expect(service.getCurrentChat()).toEqual([]);
        expect(displayed).toEqual([]);
        tick(5000);
        expect(displayed).toEqual([]);
        send("CombatDiscovered");
        tick(2000);
        expect(displayed.map((message) => message.message)).toEqual(["CombatDiscovered"]);
    }));

    it("cancels timers and unsubscribes on destruction", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        service.ngOnDestroy();
        send();
        tick(5000);
        expect(displayed).toEqual([]);
        expect(service.getCurrentChat().length).toBe(1);
    }));

    it("persists the selected options locally", () => {
        service.setEventGrouping(true, 3);
        expect(localStorage.getItem(enabledKey)).toBe("true");
        expect(localStorage.getItem(delayKey)).toBe("3");
        restartService();
        expect(service.eventGrouping).toEqual({ enabled: true, delaySeconds: 3 });
    });

    it("warns and uses the default delay for invalid stored values", () => {
        localStorage.setItem(delayKey, "NaN");
        const warning = spyOn(console, "warn");
        restartService();
        expect(service.eventGrouping.delaySeconds).toBe(2);
        expect(warning).toHaveBeenCalled();
    });

    it("rejects unsupported delays explicitly", () => {
        for (const delay of [0, -1, 0.5, 6, NaN, Infinity]) {
            expect(() => service.setEventGrouping(true, delay)).toThrowError(RangeError);
        }
        expect(service.eventGrouping).toEqual({ enabled: false, delaySeconds: 2 });
    });

    it("keeps synthetic action progress and completion in sync with the display", fakeAsync(() => {
        service.setEventGrouping(true, 2);
        send();
        const processingEvent = (query: string, text: string): BaseMessage => ({
            type: "event",
            timestamp: new Date().toISOString(),
            index: ++index,
            event: {
                kind: "tool_processing",
                tool_call_id: "search-1",
                name: "web_search_agent",
                content: { query, internal_tool_name: text, status: "processing" },
            },
        });
        output.next(processingEvent("first query", "search"));
        expect(displayed.length).toBe(1);
        expect(displayed[0].processingText).toBe("search processing");
        output.next(processingEvent("second query", "browse"));
        expect(displayed.length).toBe(1);
        expect(displayed[0].message).toBe("Searching: second query");
        expect(displayed[0].processingText).toBe("browse processing");
        output.next({
            type: "event",
            timestamp: new Date().toISOString(),
            index: ++index,
            event: { kind: "tool", request: [], results: [{ tool_call_id: "search-1" }] },
        });
        expect(displayed[0].processingText).toBeUndefined();
        send("Searching: second query", "action");
        expect(displayed.length).toBe(1);
        tick(2000);
        expect(displayed.length).toBe(2);
        expect(service.getCurrentChat().length).toBe(2);
    }));
});
