import { TestBed } from "@angular/core/testing";
import { Subject } from "rxjs";
import { Config, ConfigService } from "./config.service";
import { BaseMessage, TauriService } from "./tauri.service";
import { PluginUIService } from "./plugin-ui.service";

describe("VR placement configuration preview", () => {
    let output: Subject<BaseMessage>;
    let service: ConfigService;
    let send: jasmine.Spy;

    beforeEach(() => {
        output = new Subject<BaseMessage>();
        send = jasmine.createSpy("send_command").and.resolveTo();
        TestBed.configureTestingModule({
            providers: [
                { provide: TauriService, useValue: { output$: output, send_command: send } },
                { provide: PluginUIService, useValue: {} },
            ],
        });
        service = TestBed.inject(ConfigService);
    });

    it("updates settings and preview locally without saving the draft or losing unrelated values", () => {
        output.next({
            type: "running_config", timestamp: "", index: 0,
            config: { commander_name: "Test", overlay_vr_vertical_offset: 0 } as Config,
        });
        output.next({
            type: "vr_placement_state", timestamp: "", index: 1, active: true,
            config: { overlay_vr_vertical_offset: 0.1 },
        });
        expect(service.getCurrentConfig()?.overlay_vr_vertical_offset).toBe(0.1);
        expect(service.getCurrentConfig()?.commander_name).toBe("Test");
        expect(send).not.toHaveBeenCalled();
        output.next({
            type: "vr_placement_state", timestamp: "", index: 2, active: false,
            config: { overlay_vr_vertical_offset: 0 },
        });
        expect(service.getCurrentConfig()?.overlay_vr_vertical_offset).toBe(0);
        expect(send).not.toHaveBeenCalled();
    });

    it("does not construct an incomplete configuration from a preview arriving before initialization", () => {
        output.next({
            type: "vr_placement_state", timestamp: "", index: 0, active: true,
            config: { overlay_vr_vertical_offset: 0.1 },
        });
        expect(service.getCurrentConfig()).toBeNull();
    });
});
