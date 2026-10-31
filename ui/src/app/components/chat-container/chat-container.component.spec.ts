import { ComponentFixture, fakeAsync, TestBed, tick } from "@angular/core/testing";
import { BehaviorSubject, Subject } from "rxjs";
import { ChatContainerComponent } from "./chat-container.component";
import { ChatService } from "../../services/chat.service";
import { CharacterService } from "../../services/character.service";
import { BaseMessage, TauriService } from "../../services/tauri.service";
import { UIService } from "../../services/ui.service";

describe("ChatContainerComponent grouped events", () => {
    let fixture: ComponentFixture<ChatContainerComponent>;
    let service: ChatService;
    let output: Subject<BaseMessage>;
    let storedSettings: Map<string, string | null>;
    let index: number;

    beforeEach(async () => {
        const keys = ["covas.ui.chat-event-grouping.enabled", "covas.ui.chat-event-grouping.delay"];
        storedSettings = new Map(keys.map((key) => [key, localStorage.getItem(key)]));
        keys.forEach((key) => localStorage.removeItem(key));
        output = new Subject<BaseMessage>();
        index = 0;
        await TestBed.configureTestingModule({
            imports: [ChatContainerComponent],
            providers: [
                ChatService,
                { provide: TauriService, useValue: { output$: output.asObservable() } },
                { provide: UIService, useValue: { showTab: jasmine.createSpy("showTab") } },
                { provide: CharacterService, useValue: { character$: new BehaviorSubject(null) } },
            ],
        }).compileComponents();
        service = TestBed.inject(ChatService);
        fixture = TestBed.createComponent(ChatContainerComponent);
        fixture.detectChanges();
        service.setEventGrouping(true, 2);
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

    function send(message = "FleetCarrierDiscovered", role = "event"): void {
        output.next({
            type: "chat",
            timestamp: "2026-10-30T16:00:48Z",
            index: ++index,
            message,
            role,
        });
    }

    it("renders a counter after inactivity without modifying the original event name", fakeAsync(() => {
        for (let count = 0; count < 13; count++) {
            send();
        }
        fixture.detectChanges();
        const element: HTMLElement = fixture.nativeElement;
        expect(element.querySelector(".chat-entry")).toBeNull();
        tick(2000);
        fixture.detectChanges();
        expect(element.querySelectorAll(".chat-entry").length).toBe(1);
        expect(element.querySelector(".message")?.textContent).toBe("FleetCarrierDiscovered");
        expect(element.querySelector(".repeat-count")?.textContent).toBe("(x13)");
        expect(fixture.componentInstance.chat[0].message).toBe("FleetCarrierDiscovered");
    }));

    it("does not render a counter for a single occurrence", fakeAsync(() => {
        send("CombatDiscovered");
        tick(2000);
        fixture.detectChanges();
        const element: HTMLElement = fixture.nativeElement;
        expect(element.querySelectorAll(".chat-entry").length).toBe(1);
        expect(element.querySelector(".repeat-count")).toBeNull();
    }));

    it("preserves the existing event filters", fakeAsync(() => {
        send("Cargo");
        send("Cargo");
        send("FSSSignalDiscovered");
        send("CombatDiscovered");
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual(["CombatDiscovered"]);
    }));

    it("keeps event reaction styling based on the original event name", fakeAsync(() => {
        const status = spyOn(fixture.componentInstance, "getEventStatus").and.returnValue("disabled");
        send();
        send();
        tick(2000);
        fixture.detectChanges();
        const element: HTMLElement = fixture.nativeElement;
        expect(status).toHaveBeenCalledWith("FleetCarrierDiscovered");
        expect(element.querySelector(".chat-entry")?.classList.contains("event-disabled")).toBeTrue();
        expect(element.querySelector(".repeat-count")?.textContent).toBe("(x2)");
    }));

    it("keeps limited chat previews immediate and event-free", fakeAsync(() => {
        fixture.componentRef.setInput("limit", 2);
        send();
        send("first", "cmdr");
        send("second", "covas");
        send("third", "action");
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual(["second", "third"]);
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual(["second", "third"]);
    }));

    it("shows events by default", fakeAsync(() => {
        expect(fixture.componentInstance.showEvents).toBeTrue();
        send();
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.length).toBe(1);
    }));

    it("hides only events and restores grouped history without losing counters", fakeAsync(() => {
        send();
        send();
        const roles = ["cmdr", "covas", "action", "warn", "error", "quest", "npc_message", "plugin"];
        roles.forEach((role) => send(`${role} message`, role));
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.length).toBe(roles.length + 1);

        fixture.componentRef.setInput("showEvents", false);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.role)).toEqual(roles);
        const element: HTMLElement = fixture.nativeElement;
        expect(element.querySelector(".repeat-count")).toBeNull();
        expect(service.getCurrentChat().length).toBe(roles.length + 2);

        fixture.componentRef.setInput("showEvents", true);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.length).toBe(roles.length + 1);
        expect(element.querySelector(".repeat-count")?.textContent).toBe("(x2)");
    }));

    it("continues accumulating events while they are hidden", fakeAsync(() => {
        fixture.componentRef.setInput("showEvents", false);
        fixture.detectChanges();
        send();
        tick(1000);
        send();
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat).toEqual([]);
        expect(service.getCurrentChat().length).toBe(2);

        fixture.componentRef.setInput("showEvents", true);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.length).toBe(1);
        expect(fixture.componentInstance.chat[0].repeatCount).toBe(2);
    }));

    it("filters ungrouped events case-insensitively without delaying other messages", () => {
        service.setEventGrouping(false, 2);
        send();
        send("CombatDiscovered", "EVENT");
        fixture.componentRef.setInput("showEvents", false);
        send("immediate reply", "covas");
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual(["immediate reply"]);

        fixture.componentRef.setInput("showEvents", true);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual([
            "FleetCarrierDiscovered", "CombatDiscovered", "immediate reply",
        ]);
    });

    it("keeps built-in filters active when events are shown again", fakeAsync(() => {
        fixture.componentRef.setInput("showEvents", false);
        fixture.detectChanges();
        send("Cargo");
        send("CombatDiscovered");
        tick(2000);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat).toEqual([]);

        fixture.componentRef.setInput("showEvents", true);
        fixture.detectChanges();
        expect(fixture.componentInstance.chat.map((message) => message.message)).toEqual(["CombatDiscovered"]);
    }));
});
