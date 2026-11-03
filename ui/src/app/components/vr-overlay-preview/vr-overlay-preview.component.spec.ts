import { ComponentFixture, TestBed } from "@angular/core/testing";
import { VrOverlayPreviewComponent } from "./vr-overlay-preview.component";
import { TranslateModule } from "@ngx-translate/core";

describe("VrOverlayPreviewComponent", () => {
    let fixture: ComponentFixture<VrOverlayPreviewComponent>;
    let config: VrOverlayPreviewComponent["config"];
    let root: HTMLElement;

    beforeEach(async () => {
        await TestBed.configureTestingModule({
            imports: [VrOverlayPreviewComponent, TranslateModule.forRoot()],
        }).compileComponents();
        fixture = TestBed.createComponent(VrOverlayPreviewComponent);
        root = fixture.nativeElement;
        config = {
            overlay_vr_anchor: "head",
            overlay_vr_horizontal_offset: 0,
            overlay_vr_vertical_offset: 0,
            overlay_vr_distance_offset: 0,
            overlay_vr_tilt_degrees: 0,
            overlay_vr_yaw_degrees: 0,
            overlay_vr_size_meters: 0.9,
            overlay_vr_curvature: 0,
            overlay_vr_gaze_zoom: false,
            overlay_vr_gaze_reduction_factor: 5,
        };
        update({});
    });

    function update(changes: Partial<typeof config>): void {
        config = { ...config, ...changes };
        fixture.componentRef.setInput("config", config);
        fixture.detectChanges();
    }

    function points(selector = ".overlay-window"): number[][] {
        return root.querySelector(selector)!.getAttribute("points")!
            .split(" ").map(point => point.split(",").map(Number));
    }

    function bounds(selector = ".overlay-window") {
        const vertices = points(selector);
        const xs = vertices.map(point => point[0]);
        const ys = vertices.map(point => point[1]);
        const left = Math.min(...xs);
        const right = Math.max(...xs);
        const top = Math.min(...ys);
        const bottom = Math.max(...ys);
        return { x: (left + right) / 2, y: (top + bottom) / 2, width: right - left, height: bottom - top };
    }

    it("renders an accessible empty orange reference frame, its center axes, and a blue window above them", () => {
        const svg = root.querySelector("svg")!;
        const reference = root.querySelector(".reference")!;
        expect(svg.getAttribute("role")).toBe("img");
        expect(svg.getAttribute("aria-label")).toContain("VR placement preview");
        expect(reference.querySelectorAll("rect").length).toBe(1);
        expect(reference.querySelectorAll("line").length).toBe(2);
        expect(reference.querySelector("line")!.getAttribute("y1")).toBe("180");
        expect(reference.querySelectorAll("line")[1].getAttribute("x1")).toBe("320");
        expect(reference.nextElementSibling).toBe(root.querySelector(".overlay-window"));
        expect(getComputedStyle(reference).fill).toBe("none");
        expect(getComputedStyle(reference).stroke).toBe("rgb(255, 117, 0)");
        expect(getComputedStyle(root.querySelector("polygon")!).stroke).toBe("rgb(66, 165, 245)");
    });

    it("halves both preview dimensions at the runtime's 1.1 m head-locked distance", () => {
        const box = bounds();
        expect(box.x).toBe(320);
        expect(box.y).toBe(180);
        expect(box.width).toBeCloseTo(320 * 0.9 / 1.1 / 2, 1);
        expect(box.height).toBeCloseTo(320 * 0.9 * 9 / 16 / 1.1 / 2, 1);
        expect(box.width / box.height).toBeCloseTo(16 / 9, 3);
    });

    it("halves horizontal and vertical displacements in both directions", () => {
        update({ overlay_vr_horizontal_offset: 0.5, overlay_vr_vertical_offset: 0.25 });
        expect(bounds().x).toBeCloseTo(320 + 320 * 0.5 / 1.1 / 2, 1);
        expect(bounds().y).toBeCloseTo(180 - 320 * 0.25 / 1.1 / 2, 1);
        update({ overlay_vr_horizontal_offset: -0.5, overlay_vr_vertical_offset: -0.25 });
        expect(bounds().x).toBeCloseTo(320 - 320 * 0.5 / 1.1 / 2, 1);
        expect(bounds().y).toBeCloseTo(180 + 320 * 0.25 / 1.1 / 2, 1);
    });

    it("scales with size and inverse distance while keeping the orange reference fixed", () => {
        const reference = root.querySelector(".reference")!.outerHTML;
        const originalWidth = bounds().width;
        update({ overlay_vr_distance_offset: 1.1 });
        expect(bounds().width).toBeCloseTo(originalWidth / 2, 1);
        update({ overlay_vr_size_meters: 1.8 });
        expect(bounds().width).toBeCloseTo(originalWidth, 1);
        expect(root.querySelector(".reference")!.outerHTML).toBe(reference);
    });

    it("applies pitch perspective with the same sign as the runtime", () => {
        update({ overlay_vr_tilt_degrees: 45 });
        const vertices = points();
        const topWidth = vertices[1][0] - vertices[0][0];
        const bottomWidth = vertices[2][0] - vertices[3][0];
        expect(topWidth).toBeGreaterThan(bottomWidth);
        update({ overlay_vr_tilt_degrees: -45 });
        const reversed = points();
        expect(reversed[1][0] - reversed[0][0]).toBeLessThan(reversed[2][0] - reversed[3][0]);
    });

    it("applies yaw perspective with the same sign as the runtime", () => {
        update({ overlay_vr_yaw_degrees: 45 });
        const vertices = points();
        const leftHeight = vertices[3][1] - vertices[0][1];
        const rightHeight = vertices[2][1] - vertices[1][1];
        expect(leftHeight).toBeGreaterThan(rightHeight);
        update({ overlay_vr_yaw_degrees: -45 });
        const reversed = points();
        expect(reversed[3][1] - reversed[0][1]).toBeLessThan(reversed[2][1] - reversed[1][1]);
    });

    it("composes pitch before yaw rather than rotating the 2D rectangle", () => {
        update({ overlay_vr_tilt_degrees: 30, overlay_vr_yaw_degrees: 40 });
        const pitch = Math.PI / 6;
        const yaw = 40 * Math.PI / 180;
        const x = -0.45;
        const y = 0.9 * 9 / 16 / 2;
        const depth = 1.1 + x * Math.sin(yaw) - y * Math.sin(pitch) * Math.cos(yaw);
        expect(points()[0][0]).toBeCloseTo(320 + 160 * (x * Math.cos(yaw) + y * Math.sin(pitch) * Math.sin(yaw)) / depth, 1);
        expect(points()[0][1]).toBeCloseTo(180 - 160 * y * Math.cos(pitch) / depth, 1);
    });

    it("uses the world-locked runtime offsets and distance without explanatory paragraphs", () => {
        update({ overlay_vr_anchor: "world" });
        expect(bounds().x).toBe(320 - 160 * 0.5 / 2);
        expect(bounds().y).toBe(180 + 160 * (720 / 1280) / 2 / 2);
        expect(bounds().width).toBeCloseTo(160 * 0.9 / 2, 1);
        expect(root.querySelector("p")).toBeNull();
        update({ overlay_vr_anchor: "head" });
        expect(root.querySelector("p")).toBeNull();
    });

    it("halves world-locked offset changes without changing configuration or the orange frame", () => {
        update({ overlay_vr_anchor: "world" });
        const original = bounds();
        const reference = root.querySelector(".reference")!.outerHTML;
        const changes = { overlay_vr_horizontal_offset: 1, overlay_vr_vertical_offset: -0.5 };
        update(changes);
        const savedConfig = { ...config };
        expect(bounds().x - original.x).toBeCloseTo(320 * changes.overlay_vr_horizontal_offset / 2 / 2, 1);
        expect(bounds().y - original.y).toBeCloseTo(-320 * changes.overlay_vr_vertical_offset / 2 / 2, 1);
        expect(bounds().width).toBeCloseTo(original.width, 1);
        expect(bounds().height).toBeCloseTo(original.height, 1);
        expect(root.querySelector(".reference")!.outerHTML).toBe(reference);
        expect(config).toEqual(savedConfig);
    });

    it("curves the projected window and updates even when a bound config object is mutated", () => {
        const flat = points();
        config.overlay_vr_curvature = 0.25;
        fixture.detectChanges();
        expect(points().length).toBeGreaterThan(flat.length);
        expect(points()[0][1]).toBeLessThan(flat[0][1]);
        expect(points()[0][0]).not.toBe(flat[0][0]);
    });

    it("shows a centered empty blue inner frame at the actual area reduction, updating without moving the full window", () => {
        update({ overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true });
        const fullPoints = fixture.componentInstance.overlayPoints;
        const full = bounds();
        for (const factor of [1, 5, 9, 12.5]) {
            update({ overlay_vr_gaze_reduction_factor: factor });
            const reduced = bounds(".reduced-window");
            expect(reduced.x).toBeCloseTo(full.x, 1);
            expect(reduced.y).toBeCloseTo(full.y, 1);
            expect(reduced.width / full.width).toBeCloseTo(1 / Math.sqrt(factor), 3);
            expect(reduced.height / full.height).toBeCloseTo(1 / Math.sqrt(factor), 3);
            expect(reduced.width * reduced.height / (full.width * full.height)).toBeCloseTo(1 / factor, 3);
            expect(fixture.componentInstance.overlayPoints).toBe(fullPoints);
        }
        const frame = root.querySelector(".reduced-window")!;
        expect(getComputedStyle(frame).fill).toBe("none");
        expect(getComputedStyle(frame).stroke).toBe("rgb(66, 165, 245)");
        expect(frame.querySelector("title")).not.toBeNull();
    });

    it("projects the reduced window with the same offsets, pitch, yaw and curvature as the runtime", () => {
        update({
            overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 9,
            overlay_vr_horizontal_offset: 0.7, overlay_vr_vertical_offset: 0.2,
            overlay_vr_distance_offset: -0.5, overlay_vr_tilt_degrees: 30,
            overlay_vr_yaw_degrees: -40, overlay_vr_curvature: 0.25,
        });
        const reducedPoints = fixture.componentInstance.reducedOverlayPoints;
        if (reducedPoints === null) throw new Error("Expected the reduced-size outline for a world-locked adaptive window.");
        update({ overlay_vr_size_meters: config.overlay_vr_size_meters / 3 });
        expect(fixture.componentInstance.overlayPoints).toBe(reducedPoints);
    });

    it("hides the reduced frame and legend when reduction is disabled, head locked, or the factor is invalid", () => {
        update({ overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true });
        expect(root.querySelector(".reduced-key")).not.toBeNull();
        for (const changes of [
            { overlay_vr_gaze_zoom: false },
            { overlay_vr_gaze_zoom: true, overlay_vr_anchor: "head" as const },
            ...[0, 0.5, NaN, Infinity].map(factor => ({
                overlay_vr_anchor: "world" as const, overlay_vr_gaze_reduction_factor: factor,
            })),
        ]) {
            update(changes);
            expect(root.querySelector(".reduced-window")).toBeNull();
            expect(root.querySelector(".reduced-key")).toBeNull();
        }
    });

    it("keeps projections finite at all slider extremes, including windows crossing the viewer plane", () => {
        for (const anchor of ["head", "world"] as const) {
            for (const pitch of [-90, 0, 90]) {
                for (const yaw of [-90, 0, 90]) {
                    for (const curvature of [0, 0.5]) {
                        for (const size of [0.1, 3]) {
                            for (const distance of [-1.5, 1.5]) {
                                update({
                                    overlay_vr_anchor: anchor,
                                    overlay_vr_horizontal_offset: 2,
                                    overlay_vr_vertical_offset: -1.5,
                                    overlay_vr_tilt_degrees: pitch,
                                    overlay_vr_yaw_degrees: yaw,
                                    overlay_vr_curvature: curvature,
                                    overlay_vr_size_meters: size,
                                    overlay_vr_distance_offset: distance,
                                });
                                expect(points().flat().every(Number.isFinite)).toBeTrue();
                            }
                        }
                    }
                }
            }
        }
    });
});
