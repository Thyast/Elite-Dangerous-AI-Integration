import { TestBed } from "@angular/core/testing";
import { NoopAnimationsModule } from "@angular/platform-browser/animations";
import { MatSnackBar } from "@angular/material/snack-bar";
import { BehaviorSubject, of } from "rxjs";
import { ActionsSettingsComponent } from "./actions-settings.component";
import { Config, ConfigService } from "../../services/config.service";

describe("Actions settings with automatic language packs", () => {
    it("renders game actions without legacy language selection fields or config writes", async () => {
        const configs = new BehaviorSubject<Partial<Config> | null>(null);
        const changeConfig = jasmine.createSpy("changeConfig").and.resolveTo();
        await TestBed.configureTestingModule({
            imports: [ActionsSettingsComponent, NoopAnimationsModule],
            providers: [
                { provide: ConfigService, useValue: {
                    config$: configs, keybinds$: of(null), changeConfig,
                } },
                { provide: MatSnackBar, useValue: {} },
            ],
        }).compileComponents();
        const fixture = TestBed.createComponent(ActionsSettingsComponent);
        fixture.detectChanges();

        configs.next({
            game_actions_var: true,
            allowed_actions: {},
            weapon_types: [],
            prefer_primary_bindings: false,
            discovery_primary_var: true,
            discovery_firegroup_var: 1,
        });
        expect(() => fixture.detectChanges()).not.toThrow();
        await fixture.whenStable();
        fixture.detectChanges();

        const root: HTMLElement = fixture.nativeElement;
        expect(root.textContent).toContain("Available Game Actions");
        expect(root.textContent).toContain("recenterVRView");
        expect(root.textContent).not.toContain("Additional command languages");
        expect(changeConfig).not.toHaveBeenCalled();
        fixture.destroy();
    });
});
