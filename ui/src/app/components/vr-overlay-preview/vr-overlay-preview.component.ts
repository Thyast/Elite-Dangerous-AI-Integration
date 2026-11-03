import { Component, Input } from "@angular/core";
import { Config } from "../../services/config.service";
import { TranslateModule } from "@ngx-translate/core";

type PreviewConfig = Pick<Config,
    | "overlay_vr_anchor"
    | "overlay_vr_horizontal_offset"
    | "overlay_vr_vertical_offset"
    | "overlay_vr_distance_offset"
    | "overlay_vr_tilt_degrees"
    | "overlay_vr_yaw_degrees"
    | "overlay_vr_size_meters"
    | "overlay_vr_curvature"
    | "overlay_vr_gaze_zoom"
    | "overlay_vr_gaze_reduction_factor"
>;

interface Point3D {
    x: number;
    y: number;
    z: number;
}

@Component({
    selector: "app-vr-overlay-preview",
    standalone: true,
    imports: [TranslateModule],
    templateUrl: "./vr-overlay-preview.component.html",
    styleUrl: "./vr-overlay-preview.component.css",
})
export class VrOverlayPreviewComponent {
    @Input({ required: true }) config!: PreviewConfig;

    get overlayPoints(): string {
        return this.projectWindow(1);
    }

    get reducedOverlayPoints(): string | null {
        const { overlay_vr_anchor: anchor, overlay_vr_gaze_zoom: enabled, overlay_vr_gaze_reduction_factor: factor } = this.config;
        return anchor === "world" && enabled && Number.isFinite(factor) && factor >= 1
            ? this.projectWindow(1 / Math.sqrt(factor)) : null;
    }

    private projectWindow(scale: number): string {
        const config = this.config;
        const worldLocked = config.overlay_vr_anchor === "world";
        // Match Electron placement, relative to a forward-facing viewer at 1.4 m.
        const offsetX = config.overlay_vr_horizontal_offset - (worldLocked ? 0.5 : 0);
        const offsetY = config.overlay_vr_vertical_offset - (worldLocked ? (720 / 1280) / 2 : 0);
        const distance = Math.max(0.1, (worldLocked ? 2 : 1.1) + config.overlay_vr_distance_offset);
        const pitch = config.overlay_vr_tilt_degrees * Math.PI / 180;
        const yaw = config.overlay_vr_yaw_degrees * Math.PI / 180;
        const width = config.overlay_vr_size_meters * scale;
        const halfHeight = width * 9 / 16 / 2;
        const arc = config.overlay_vr_curvature * 2 * Math.PI;
        const segments = arc > 0 ? 32 : 1;
        const outline: Point3D[] = [];

        const transform = (fraction: number, y: number): Point3D => {
            const angle = fraction * arc;
            const x = arc > 0 ? width / arc * Math.sin(angle) : fraction * width;
            const z = arc > 0 ? width / arc * (1 - Math.cos(angle)) : 0;
            const pitchedY = y * Math.cos(pitch) - z * Math.sin(pitch);
            const pitchedZ = y * Math.sin(pitch) + z * Math.cos(pitch);
            // The runtime quaternion applies pitch first, then yaw.
            return {
                x: offsetX + x * Math.cos(yaw) + pitchedZ * Math.sin(yaw),
                y: offsetY + pitchedY,
                z: -distance - x * Math.sin(yaw) + pitchedZ * Math.cos(yaw),
            };
        };

        for (let i = 0; i <= segments; i++) {
            outline.push(transform(i / segments - 0.5, halfHeight));
        }
        for (let i = segments; i >= 0; i--) {
            outline.push(transform(i / segments - 0.5, -halfHeight));
        }

        // Half-scale projection reduces both the window and its offsets, not the VR placement.
        const projectionScale = 160;
        return this.clipNearPlane(outline)
            .map(point => `${(320 + projectionScale * point.x / -point.z).toFixed(2)},${(180 - projectionScale * point.y / -point.z).toFixed(2)}`)
            .join(" ");
    }

    private clipNearPlane(points: Point3D[]): Point3D[] {
        // Large, close, rotated windows can cross the viewer's plane.
        const near = -0.01;
        const clipped: Point3D[] = [];
        let previous = points[points.length - 1];
        for (const point of points) {
            if ((point.z <= near) !== (previous.z <= near)) {
                const t = (near - previous.z) / (point.z - previous.z);
                clipped.push({
                    x: previous.x + t * (point.x - previous.x),
                    y: previous.y + t * (point.y - previous.y),
                    z: near,
                });
            }
            if (point.z <= near) {
                clipped.push(point);
            }
            previous = point;
        }
        return clipped;
    }
}
