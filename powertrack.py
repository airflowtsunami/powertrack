"""
POWER TRACK: 90s STADIUM FRONT-WHEEL EDITION

Two-rider FTP-normalised cycling race with:
- identical starts and identical speed at equal % FTP
- live leader and gap display
- black road, white centre line and grass verge
- grandstand positioned above, not underneath, the road

Controls
--------
Rider 1: W/S adjust power, A reset to 100% FTP
Rider 2: Up/Down adjust power, Left reset to 100% FTP
Space: pause
R: restart
1: fullscreen
Esc: quit
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import re
from time import monotonic

import pygame

from ant_power import AntPowerManager
from pygame.math import Vector2


VW, VH = 960, 540
WINDOW_W, WINDOW_H = 1280, 720
FPS = 60
LAPS_TO_WIN = 1

HUD_W = 228
TRACK_WIDTH = 82
LANE_OFFSET = 15
TRACK_SAMPLES = 1300
TIE_EPSILON_METRES = 0.02
TIE_EPSILON_SECONDS = 0.01
FRONT_WHEEL_LEAD = 31.0

# Palette
INK = (7, 11, 18)
ROAD = (24, 27, 32)
ROAD_HI = (39, 43, 49)
ROAD_EDGE = (10, 12, 15)
KERB_RED = (224, 51, 56)
WHITE = (247, 248, 241)
MUTED = (153, 178, 203)
BLUE = (44, 133, 236)
CYAN = (74, 216, 255)
RED = (236, 59, 67)
YELLOW = (255, 220, 63)
ORANGE = (255, 128, 43)
GREEN = (82, 218, 106)
PURPLE = (166, 87, 231)
PINK = (245, 92, 177)
NAVY = (10, 19, 34)
PANEL = (14, 28, 49)
PANEL_2 = (21, 40, 67)
PANEL_EDGE = (89, 153, 208)
GRASS = (55, 157, 80)
GRASS_DARK = (31, 111, 64)
GRASS_LIGHT = (92, 190, 96)
WATER = (36, 139, 214)
WATER_HI = (126, 228, 255)
WOOD = (117, 73, 38)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def catmull_rom(p0: Vector2, p1: Vector2, p2: Vector2, p3: Vector2, t: float) -> Vector2:
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        2 * p1
        + (-p0 + p2) * t
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
        + (-p0 + 3 * p1 - 3 * p2 + p3) * t3
    )


class Track:
    """
    Exact stadium-shaped oval made from two straights and two semicircles.

    The path is parameterised by physical distance. This means:
    - tangent continuity at every join
    - consistent lane offsets
    - kerb segments with equal physical lengths
    - no malformed corners
    """

    def __init__(self) -> None:
        self.left_x = 405.0
        self.right_x = 745.0
        self.centre_y = 325.0
        self.radius = 126.0

        self.straight_length = self.right_x - self.left_x
        self.arc_length = math.pi * self.radius
        self.length = 2 * self.straight_length + 2 * self.arc_length

        # Dense centreline cache for pygame line rendering.
        self.points = []
        samples = 1200
        for index in range(samples):
            distance = self.length * index / samples
            point, _ = self.sample(distance)
            self.points.append(point)

        self.lengths = [self.length * index / samples for index in range(samples + 1)]

    def sample(self, distance: float, offset: float = 0.0) -> tuple[Vector2, Vector2]:
        """Return exact point and tangent at distance around the stadium."""
        distance %= self.length

        # Top straight: left to right.
        if distance < self.straight_length:
            point = Vector2(self.left_x + distance, self.centre_y - self.radius)
            tangent = Vector2(1, 0)

        # Right semicircle: top to bottom.
        elif distance < self.straight_length + self.arc_length:
            local = distance - self.straight_length
            angle = -math.pi / 2 + local / self.radius
            point = Vector2(
                self.right_x + self.radius * math.cos(angle),
                self.centre_y + self.radius * math.sin(angle),
            )
            tangent = Vector2(-math.sin(angle), math.cos(angle))

        # Bottom straight: right to left.
        elif distance < 2 * self.straight_length + self.arc_length:
            local = distance - (self.straight_length + self.arc_length)
            point = Vector2(self.right_x - local, self.centre_y + self.radius)
            tangent = Vector2(-1, 0)

        # Left semicircle: bottom to top.
        else:
            local = distance - (2 * self.straight_length + self.arc_length)
            angle = math.pi / 2 + local / self.radius
            point = Vector2(
                self.left_x + self.radius * math.cos(angle),
                self.centre_y + self.radius * math.sin(angle),
            )
            tangent = Vector2(-math.sin(angle), math.cos(angle))

        tangent = tangent.normalize()
        normal = Vector2(-tangent.y, tangent.x)
        return point + normal * offset, tangent

    def polygon(self, offset: float) -> list[tuple[int, int]]:
        points = []
        samples = 720
        for index in range(samples):
            point, _ = self.sample(self.length * index / samples, offset)
            points.append((round(point.x), round(point.y)))
        return points

    def curved_strip_polygon(
        self,
        start_distance: float,
        end_distance: float,
        inner_offset: float,
        outer_offset: float,
        step: float = 2.0,
    ) -> list[tuple[int, int]]:
        """
        Build a filled strip polygon following the exact oval.

        Used for kerbs so the red/white blocks are filled areas, not thick
        lines that distort around bends.
        """
        outer = []
        inner = []

        distance = start_distance
        while distance < end_distance:
            point, _ = self.sample(distance, outer_offset)
            outer.append((round(point.x), round(point.y)))
            distance += step

        point, _ = self.sample(end_distance, outer_offset)
        outer.append((round(point.x), round(point.y)))

        distance = end_distance
        while distance > start_distance:
            point, _ = self.sample(distance, inner_offset)
            inner.append((round(point.x), round(point.y)))
            distance -= step

        point, _ = self.sample(start_distance, inner_offset)
        inner.append((round(point.x), round(point.y)))

        return outer + inner

    def band_mask(self, offset_a: float, offset_b: float) -> pygame.Surface:
        """
        Return an exact alpha mask for the stadium band between two offsets.

        The larger physical stadium boundary is filled first and the smaller
        boundary is cut out. This produces a true ring with no line-join gaps.
        """
        mask = pygame.Surface((VW, VH), pygame.SRCALPHA)

        # Negative offsets are farther outside; positive offsets are inward.
        outer_offset = min(offset_a, offset_b)
        inner_offset = max(offset_a, offset_b)

        pygame.draw.polygon(
            mask,
            (255, 255, 255, 255),
            self.polygon(outer_offset),
        )
        pygame.draw.polygon(
            mask,
            (0, 0, 0, 0),
            self.polygon(inner_offset),
        )
        return mask

    def fill_band(
        self,
        surface: pygame.Surface,
        offset_a: float,
        offset_b: float,
        colour: tuple[int, int, int],
    ) -> None:
        """Fill an exact stadium band without affecting its centre hole."""
        mask = self.band_mask(offset_a, offset_b)
        layer = pygame.Surface((VW, VH), pygame.SRCALPHA)
        layer.fill((*colour, 255))
        layer.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surface.blit(layer, (0, 0))

    def draw(self, surface: pygame.Surface) -> None:
        """
        Exact, continuous 1990s stadium circuit.

        The road is rendered as one ring mask rather than as a thick line.
        Consequently, every black road pixel is mathematically constrained
        between the two kerb edges.
        """
        road_half_width = 43.0
        kerb_width = 12.0
        retaining_width = 6.0

        # Exact shadow ring.
        shadow_mask = self.band_mask(
            -(road_half_width + kerb_width + retaining_width),
            +(road_half_width + kerb_width + retaining_width),
        )
        shadow = pygame.Surface((VW, VH), pygame.SRCALPHA)
        shadow.fill((0, 0, 0, 88))
        shadow.blit(shadow_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        shifted_shadow = pygame.Surface((VW, VH), pygame.SRCALPHA)
        shifted_shadow.blit(shadow, (8, 10))
        surface.blit(shifted_shadow, (0, 0))

        # Dark retaining ring beneath kerbs.
        self.fill_band(
            surface,
            -(road_half_width + kerb_width + retaining_width),
            +(road_half_width + kerb_width + retaining_width),
            (8, 11, 15),
        )

        # Continuous white kerb bands on both edges.
        self.fill_band(
            surface,
            -(road_half_width + kerb_width),
            -road_half_width,
            WHITE,
        )
        self.fill_band(
            surface,
            road_half_width,
            road_half_width + kerb_width,
            WHITE,
        )

        # Alternating red kerb polygons. Each side is phase-shifted.
        block_length = 20.0
        for side_index, side in enumerate((-1, 1)):
            if side < 0:
                inner_offset = -(road_half_width + kerb_width)
                outer_offset = -road_half_width
            else:
                inner_offset = road_half_width
                outer_offset = road_half_width + kerb_width

            block_index = 0
            start_distance = 0.0
            while start_distance < self.length:
                end_distance = min(start_distance + block_length, self.length)
                if (block_index + side_index) % 2 == 0:
                    polygon = self.curved_strip_polygon(
                        start_distance,
                        end_distance,
                        inner_offset,
                        outer_offset,
                        step=1.25,
                    )
                    pygame.draw.polygon(surface, KERB_RED, polygon)

                block_index += 1
                start_distance = end_distance

        # Exact uninterrupted road ring. Drawn after kerbs to guarantee a
        # perfectly clean inner kerb boundary.
        road_mask = self.band_mask(-road_half_width, road_half_width)

        road = pygame.Surface((VW, VH), pygame.SRCALPHA)
        road.fill((30, 35, 44, 255))
        road.blit(road_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        # Layered 90s arcade colouring, all clipped to the same road mask.
        colour_layer = pygame.Surface((VW, VH), pygame.SRCALPHA)
        colour_layer.fill((0, 0, 0, 0))

        # Outer blue-grey road shoulders.
        shoulder_mask = self.band_mask(-36.0, 36.0)
        shoulder = pygame.Surface((VW, VH), pygame.SRCALPHA)
        shoulder.fill((48, 57, 70, 255))
        shoulder.blit(shoulder_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        colour_layer.blit(shoulder, (0, 0))

        # Darker central racing strip.
        centre_mask = self.band_mask(-20.0, 20.0)
        centre_strip = pygame.Surface((VW, VH), pygame.SRCALPHA)
        centre_strip.fill((38, 42, 54, 255))
        centre_strip.blit(centre_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        colour_layer.blit(centre_strip, (0, 0))

        # Purple centre tint.
        purple_mask = self.band_mask(-8.0, 8.0)
        purple_strip = pygame.Surface((VW, VH), pygame.SRCALPHA)
        purple_strip.fill((53, 44, 67, 255))
        purple_strip.blit(purple_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        colour_layer.blit(purple_strip, (0, 0))

        road.blit(colour_layer, (0, 0))
        surface.blit(road, (0, 0))

        # Fine white road-edge markers just inside the kerbs.
        for side in (-1, 1):
            pygame.draw.lines(
                surface,
                (225, 230, 232),
                True,
                self.polygon(side * (road_half_width - 4)),
                2,
            )

        # Curved tyre marks, safely inside the exact road ring.
        skid_layer = pygame.Surface((VW, VH), pygame.SRCALPHA)
        for lane_offset, alpha, start_fraction, run_length in [
            (-22, 72, 0.03, 155),
            (21, 68, 0.50, 150),
            (-11, 42, 0.72, 95),
            (10, 36, 0.25, 80),
        ]:
            start_distance = self.length * start_fraction
            end_distance = start_distance + run_length
            distance = start_distance

            while distance < end_distance:
                points = []
                section_end = min(distance + 17, end_distance)
                sample_distance = distance
                while sample_distance <= section_end:
                    point, _ = self.sample(sample_distance, lane_offset)
                    points.append((round(point.x), round(point.y)))
                    sample_distance += 2.0

                if len(points) >= 2:
                    pygame.draw.lines(
                        skid_layer,
                        (0, 0, 0, alpha),
                        False,
                        points,
                        3,
                    )
                distance += 20

        skid_layer.blit(road_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surface.blit(skid_layer, (0, 0))

        # Dashed white centre line following exact stadium geometry.
        dash_length = 23.0
        dash_gap = 27.0
        distance = 0.0
        while distance < self.length:
            points = []
            dash_end = min(distance + dash_length, self.length)
            sample_distance = distance

            while sample_distance <= dash_end:
                point, _ = self.sample(sample_distance)
                points.append((round(point.x), round(point.y)))
                sample_distance += 1.75

            if len(points) >= 2:
                pygame.draw.lines(surface, WHITE, False, points, 4)

            distance += dash_length + dash_gap

        # Starting grid boxes on the upper straight.
        for box_index in range(7):
            box_distance = 36 + box_index * 40
            centre, tangent = self.sample(box_distance)
            normal = Vector2(-tangent.y, tangent.x)

            for side in (-1, 1):
                box_centre = centre + normal * side * 20
                corners = [
                    box_centre - tangent * 9 - normal * 6,
                    box_centre + tangent * 9 - normal * 6,
                    box_centre + tangent * 9 + normal * 6,
                    box_centre - tangent * 9 + normal * 6,
                ]
                pygame.draw.lines(
                    surface,
                    (222, 227, 230),
                    True,
                    [(round(point.x), round(point.y)) for point in corners],
                    2,
                )

        # Start/finish checker stripe.
        centre, tangent = self.sample(0)
        normal = Vector2(-tangent.y, tangent.x)
        for row in range(4):
            for col in range(14):
                colour = WHITE if (row + col) % 2 == 0 else INK
                point = (
                    centre
                    + tangent * (row * 6 - 11)
                    + normal * (
                        -road_half_width
                        + (col + 0.5) * road_half_width * 2 / 14
                    )
                )
                pygame.draw.rect(
                    surface,
                    colour,
                    pygame.Rect(
                        round(point.x) - 3,
                        round(point.y) - 3,
                        7,
                        6,
                    ),
                )


@dataclass
class Rider:
    name: str
    ftp: int
    power: float
    primary: tuple[int, int, int]
    secondary: tuple[int, int, int]
    lane: float
    distance: float = 0.0
    speed_mps: float = 0.0
    lap: int = 1
    finished: bool = False
    finish_time: float | None = None
    pulse: float = 0.0
    wave_phase: float = 0.0
    skin: tuple[int, int, int] = (239, 184, 137)
    hair: tuple[int, int, int] = (42, 31, 28)
    power_samples: list[tuple[float, float]] | None = None
    best_60s_power: float = 0.0
    total_power_integral: float = 0.0
    power_sample_duration: float = 0.0

    def __post_init__(self) -> None:
        if self.power_samples is None:
            self.power_samples = []

    @property
    def intensity(self) -> float:
        return self.power / max(1, self.ftp)

    def record_power(self, elapsed: float, dt: float) -> None:
        """
        Record live power and calculate the race's one-minute test value.

        Before 60 seconds, use the best average over the complete effort so
        far. From 60 seconds onward, use the best rolling 60-second average.
        """
        if dt <= 0.0:
            return

        watts = max(0.0, float(self.power))
        self.power_samples.append((elapsed, watts))
        self.total_power_integral += watts * dt
        self.power_sample_duration += dt

        if self.power_sample_duration < 60.0:
            effort_average = (
                self.total_power_integral
                / max(self.power_sample_duration, 0.000001)
            )
            self.best_60s_power = max(
                self.best_60s_power,
                effort_average,
            )
            return

        cutoff = elapsed - 60.0
        while (
            len(self.power_samples) > 2
            and self.power_samples[1][0] <= cutoff
        ):
            self.power_samples.pop(0)

        samples = self.power_samples
        integral = 0.0

        for index in range(len(samples) - 1):
            t0, p0 = samples[index]
            t1, p1 = samples[index + 1]

            segment_start = max(t0, cutoff)
            segment_end = min(t1, elapsed)
            if segment_end <= segment_start or t1 <= t0:
                continue

            start_ratio = (segment_start - t0) / (t1 - t0)
            end_ratio = (segment_end - t0) / (t1 - t0)
            power_start = p0 + (p1 - p0) * start_ratio
            power_end = p0 + (p1 - p0) * end_ratio

            integral += (
                (power_start + power_end)
                * 0.5
                * (segment_end - segment_start)
            )

        rolling_average = integral / 60.0
        self.best_60s_power = max(
            self.best_60s_power,
            rolling_average,
        )

    @property
    def average_power(self) -> float:
        if self.power_sample_duration <= 0.0:
            return 0.0
        return self.total_power_integral / self.power_sample_duration

    @property
    def suggested_ftp(self) -> float:
        return self.best_60s_power * 0.75

    def target_speed(self) -> float:
        """
        One consistent FTP-normalised speed formula from 0% to 500%.

        There is no special diminishing rule above 150%; the visual power bar
        alone is logarithmic.
        """
        intensity = clamp(self.intensity, 0.0, 5.0)
        if intensity <= 0.0:
            return 0.0

        speed_kmh = 40.0 * (intensity ** 0.72)
        return speed_kmh / 3.6


    def update(self, dt: float, elapsed: float, track_length: float) -> None:
        if self.finished:
            return

        if self.power <= 0.0:
            self.speed_mps = 0.0
            return

        target = self.target_speed()
        response = 1 - math.exp(-2.25 * dt)
        self.speed_mps += (target - self.speed_mps) * response

        previous_distance = self.distance
        new_distance = self.distance + self.speed_mps * dt
        finish_distance = LAPS_TO_WIN * track_length - FRONT_WHEEL_LEAD

        if previous_distance < finish_distance <= new_distance:
            frame_distance = max(0.000001, new_distance - previous_distance)
            crossing_fraction = (
                finish_distance - previous_distance
            ) / frame_distance

            # elapsed is the end-of-frame time, so interpolate back to the
            # instant the front wheel reaches the line.
            self.finish_time = elapsed - dt + dt * crossing_fraction
            self.distance = finish_distance
            self.finished = True
            self.speed_mps = 0.0
            self.lap = LAPS_TO_WIN
            return

        self.distance = new_distance
        self.pulse += dt * (5 + self.intensity * 6)
        self.wave_phase += dt

        completed_laps = int(
            (self.distance + FRONT_WHEEL_LEAD) // track_length
        )
        self.lap = min(completed_laps + 1, LAPS_TO_WIN)


def make_font(size: int, bold: bool = False) -> pygame.font.Font:
    result = pygame.font.Font(None, size)
    result.set_bold(bold)
    return result


def make_mono_font(size: int, bold: bool = False) -> pygame.font.Font:
    """Monospaced font for timers and gap values so digits never jump."""
    result = pygame.font.SysFont("consolas", size, bold=bold)
    return result


def draw_text(surface, font, value, pos, colour=WHITE, anchor="topleft", shadow=False):
    image = font.render(value, True, colour)
    rect = image.get_rect()
    setattr(rect, anchor, pos)
    if shadow:
        shadow_image = font.render(value, True, INK)
        surface.blit(shadow_image, rect.move(2, 2))
    surface.blit(image, rect)
    return rect


def gradient_rect(surface, rect, top, bottom):
    for y in range(rect.height):
        t = y / max(1, rect.height - 1)
        colour = tuple(round(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        pygame.draw.line(surface, colour, (rect.x, rect.y + y), (rect.right - 1, rect.y + y))


def draw_panel(surface, rect, edge=PANEL_EDGE, fill=PANEL):
    pygame.draw.rect(surface, INK, rect.move(4, 5), border_radius=10)
    gradient_rect(surface, rect, PANEL_2, fill)
    pygame.draw.rect(surface, edge, rect, 2, border_radius=10)
    pygame.draw.line(
        surface,
        (136, 199, 237),
        (rect.x + 8, rect.y + 4),
        (rect.right - 8, rect.y + 4),
        1,
    )


def draw_background(surface):
    gradient_rect(
        surface,
        pygame.Rect(HUD_W, 0, VW - HUD_W, 130),
        (48, 120, 224),
        (121, 217, 248),
    )
    pygame.draw.circle(surface, (255, 240, 122), (818, 50), 34)
    pygame.draw.circle(surface, (255, 250, 189), (818, 50), 22)

    pygame.draw.polygon(
        surface,
        (78, 108, 177),
        [
            (HUD_W, 126), (310, 70), (395, 126), (493, 52), (590, 126),
            (706, 76), (782, 126), (896, 58), (960, 126),
        ],
    )
    pygame.draw.polygon(
        surface,
        (117, 87, 170),
        [
            (HUD_W, 126), (333, 91), (422, 126), (533, 72), (648, 126),
            (749, 87), (832, 126), (915, 80), (960, 126),
        ],
    )

    pygame.draw.rect(surface, GRASS, (HUD_W, 124, VW - HUD_W, VH - 124))
    for y in range(125, VH, 30):
        offset = 15 if (y // 30) % 2 else 0
        for x in range(HUD_W + offset, VW, 30):
            colour = GRASS_LIGHT if ((x // 30 + y // 30) % 2 == 0) else GRASS
            pygame.draw.rect(surface, colour, (x, y, 30, 30))

    rng = random.Random(41)
    for _ in range(2200):
        x = rng.randrange(HUD_W, VW)
        y = rng.randrange(127, VH)
        surface.set_at(
            (x, y),
            rng.choice([GRASS_DARK, GRASS_LIGHT, (110, 192, 94), (43, 130, 74)]),
        )


def draw_grandstand(surface):
    # Grandstand is entirely above the road. It ends at y=122;
    # the road's upper edge begins below approximately y=130.
    stand = pygame.Rect(HUD_W + 12, 72, VW - HUD_W - 24, 50)
    pygame.draw.rect(surface, (17, 23, 34), stand)
    pygame.draw.rect(surface, (48, 58, 75), (stand.x, stand.bottom - 9, stand.width, 9))
    pygame.draw.line(surface, WHITE, (stand.x, stand.bottom), (stand.right, stand.bottom), 2)

    rng = random.Random(12)
    palette = [RED, BLUE, YELLOW, GREEN, PURPLE, ORANGE, PINK, WHITE]
    for x in range(stand.x + 9, stand.right - 5, 11):
        y = rng.choice([82, 85, 88, 91])
        skin = rng.choice([(238, 184, 137), (176, 117, 78), (102, 67, 54)])
        shirt = rng.choice(palette)
        pygame.draw.circle(surface, skin, (x, y), 3)
        pygame.draw.rect(surface, shirt, (x - 3, y + 4, 7, 11))
        if rng.random() < 0.3:
            pygame.draw.line(surface, skin, (x - 2, y + 7), (x - 7, y + 1), 2)

    banners = [
        ("VELOMAX", BLUE), ("POWER UP!", RED), ("ANT+", GREEN),
        ("CYCLON", PURPLE), ("BIKE LAB", YELLOW), ("SPEEDTECH", CYAN),
    ]
    x = stand.x + 8
    small_font = make_font(16, True)
    for label, colour in banners:
        width = 93 if label not in ("ANT+", "BIKE LAB") else (70 if label == "ANT+" else 88)
        banner = pygame.Rect(x, 104, width, 20)
        pygame.draw.rect(surface, colour, banner)
        text_colour = INK if colour in (YELLOW, CYAN, GREEN) else WHITE
        draw_text(surface, small_font, label, banner.center, text_colour, "center")
        x += width + 9


def draw_water(surface):
    pygame.draw.ellipse(surface, (20, 81, 128), (608, 272, 151, 90))
    pygame.draw.ellipse(surface, WATER, (615, 277, 138, 80))
    for y in range(289, 344, 12):
        pygame.draw.arc(surface, WATER_HI, (625, y, 118, 18), 0.2, 2.7, 2)


def draw_tree(surface, x, y, scale=1.0):
    pygame.draw.rect(
        surface,
        WOOD,
        (round(x - 4 * scale), round(y), round(8 * scale), round(28 * scale)),
    )
    for dx, dy, radius, colour in [
        (-13, -15, 17, (48, 138, 68)),
        (0, -22, 22, (73, 173, 74)),
        (14, -15, 17, (97, 194, 82)),
        (-1, -7, 18, (58, 154, 71)),
    ]:
        pygame.draw.circle(
            surface,
            colour,
            (round(x + dx * scale), round(y + dy * scale)),
            round(radius * scale),
        )


def draw_scenery(surface):
    draw_water(surface)

    for tree in [
        (370, 235, 0.8), (566, 417, 0.8), (640, 224, 0.75),
        (720, 393, 0.85), (881, 293, 0.75), (454, 452, 0.7),
    ]:
        draw_tree(surface, *tree)

    rng = random.Random(22)
    colours = [YELLOW, PINK, WHITE, CYAN, ORANGE]
    for _ in range(150):
        x = rng.randrange(HUD_W + 20, VW - 12)
        y = rng.randrange(145, VH - 15)
        colour = rng.choice(colours)
        pygame.draw.circle(surface, colour, (x, y), 2)
        pygame.draw.line(surface, GRASS_DARK, (x, y + 2), (x, y + 5), 1)


def draw_title(surface):
    title_font = make_font(50, True)
    for y, width, colour in [(17, 110, CYAN), (25, 89, BLUE), (33, 68, (31, 83, 174))]:
        pygame.draw.polygon(
            surface,
            colour,
            [(256, y), (256 + width, y), (344, y + 8), (256, y + 8)],
        )
        pygame.draw.polygon(
            surface,
            colour,
            [(895, y), (895 - width, y), (808, y + 8), (895, y + 8)],
        )

    draw_text(surface, title_font, "POWER TRACK", (578, 12), RED, "midtop", True)
    draw_text(surface, title_font, "POWER TRACK", (574, 8), ORANGE, "midtop", True)
    draw_text(surface, title_font, "POWER TRACK", (570, 4), YELLOW, "midtop", True)
    draw_text(surface, make_font(16, True), "90s FRONT-WHEEL", (570, 58), WHITE, "midtop", True)


def draw_rider_sprite(surface, position, tangent, rider, label=True):
    """Draw a cheerful cartoon cyclist with both hands on the handlebars."""
    sprite = pygame.Surface((196, 148), pygame.SRCALPHA)

    skin = rider.skin
    skin_shadow = tuple(max(0, channel - 34) for channel in skin)
    skin_highlight = tuple(min(255, channel + 20) for channel in skin)
    hair = rider.hair

    # Shadow.
    pygame.draw.ellipse(sprite, (0, 0, 0, 76), (25, 117, 144, 18))

    # Wheels.
    wheel_y = 109
    for centre_x in (48, 148):
        pygame.draw.circle(sprite, (12, 15, 19), (centre_x, wheel_y), 31)
        pygame.draw.circle(sprite, (244, 246, 244), (centre_x, wheel_y), 25)
        pygame.draw.circle(sprite, (156, 171, 184), (centre_x, wheel_y), 23, 2)
        pygame.draw.circle(sprite, rider.secondary, (centre_x, wheel_y), 5)
        for spoke_angle in range(0, 180, 20):
            dx = round(math.cos(math.radians(spoke_angle)) * 21)
            dy = round(math.sin(math.radians(spoke_angle)) * 21)
            pygame.draw.line(
                sprite,
                (166, 177, 186),
                (centre_x - dx, wheel_y - dy),
                (centre_x + dx, wheel_y + dy),
                1,
            )

    # Bike frame.
    frame = rider.secondary
    frame_dark = tuple(max(0, channel - 65) for channel in frame)
    frame_points = [
        (48, 109), (98, 98), (148, 109),
        (120, 61), (76, 61), (48, 109),
    ]
    pygame.draw.lines(sprite, frame_dark, False, frame_points, 12)
    pygame.draw.lines(sprite, frame, False, frame_points, 8)
    pygame.draw.line(sprite, frame, (98, 98), (76, 61), 8)
    pygame.draw.line(sprite, frame, (120, 61), (148, 109), 8)
    pygame.draw.line(sprite, WHITE, (121, 57), (157, 51), 6)
    pygame.draw.line(sprite, INK, (68, 56), (91, 56), 7)

    # Pedalling legs.
    pedal_phase = math.sin(rider.pulse)
    front_foot = (65, round(94 + pedal_phase * 10))
    rear_foot = (105, round(95 - pedal_phase * 10))
    shorts = (27, 74, 107) if rider.name == "Rider 1" else (52, 61, 112)
    shoe = (248, 71, 72) if rider.name == "Rider 1" else (255, 202, 58)

    pygame.draw.line(sprite, shorts, (80, 65), front_foot, 18)
    pygame.draw.line(sprite, shorts, (111, 64), rear_foot, 17)
    pygame.draw.line(sprite, skin, (65, 86), front_foot, 10)
    pygame.draw.line(sprite, skin, (105, 87), rear_foot, 10)
    pygame.draw.ellipse(sprite, shoe, (front_foot[0] - 12, front_foot[1] - 5, 25, 11))
    pygame.draw.ellipse(sprite, shoe, (rear_foot[0] - 12, rear_foot[1] - 5, 25, 11))

    # Torso.
    pygame.draw.line(sprite, rider.primary, (79, 61), (108, 42), 35)
    pygame.draw.polygon(
        sprite,
        rider.primary,
        [(74, 49), (104, 32), (128, 54), (111, 72), (80, 69)],
    )
    pygame.draw.line(sprite, rider.secondary, (84, 48), (111, 39), 5)
    pygame.draw.line(sprite, WHITE, (91, 43), (113, 38), 4)

    # Neck and head.
    pygame.draw.rect(sprite, skin_shadow, (72, 27, 16, 21), border_radius=6)
    pygame.draw.ellipse(sprite, hair, (36, 0, 64, 57))
    pygame.draw.ellipse(sprite, skin_shadow, (42, 3, 61, 57))
    pygame.draw.ellipse(sprite, skin, (39, 0, 61, 56))
    pygame.draw.circle(sprite, skin, (42, 29), 10)
    pygame.draw.arc(sprite, skin_shadow, (35, 21, 14, 16), -0.8, 1.8, 2)

    pygame.draw.polygon(
        sprite,
        hair,
        [(42, 8), (50, 1), (62, 6), (73, 0), (87, 8), (96, 10),
         (91, 17), (78, 12), (68, 17), (56, 12), (47, 17)],
    )

    # Helmet.
    helmet = CYAN if rider.name == "Rider 1" else YELLOW
    helmet_dark = BLUE if rider.name == "Rider 1" else ORANGE
    pygame.draw.arc(sprite, helmet_dark, (31, -7, 75, 45), math.pi, 2 * math.pi, 18)
    pygame.draw.arc(sprite, helmet, (34, -9, 69, 39), math.pi, 2 * math.pi, 13)
    for x in (49, 64, 79, 92):
        pygame.draw.line(sprite, helmet_dark, (x, 0), (x - 5, 11), 3)
    pygame.draw.line(sprite, (55, 60, 67), (38, 22), (95, 22), 6)
    pygame.draw.line(sprite, (55, 60, 67), (42, 22), (47, 40), 3)
    pygame.draw.line(sprite, (55, 60, 67), (94, 22), (88, 42), 3)

    # Face.
    eye_y = 25
    pygame.draw.arc(sprite, hair, (51, 16, 16, 10), math.pi, 2 * math.pi, 3)
    pygame.draw.arc(sprite, hair, (76, 16, 16, 10), math.pi, 2 * math.pi, 3)
    pygame.draw.ellipse(sprite, WHITE, (52, eye_y, 15, 14))
    pygame.draw.ellipse(sprite, WHITE, (76, eye_y, 15, 14))
    pygame.draw.circle(sprite, (57, 39, 34), (62, eye_y + 7), 4)
    pygame.draw.circle(sprite, (57, 39, 34), (82, eye_y + 7), 4)
    pygame.draw.circle(sprite, WHITE, (63, eye_y + 5), 1)
    pygame.draw.circle(sprite, WHITE, (83, eye_y + 5), 1)
    pygame.draw.ellipse(sprite, skin_highlight, (66, 34, 14, 8))
    pygame.draw.circle(sprite, (246, 128, 130), (53, 40), 5)
    pygame.draw.circle(sprite, (246, 128, 130), (90, 40), 5)
    pygame.draw.ellipse(sprite, (90, 31, 30), (61, 40, 25, 13))
    pygame.draw.arc(sprite, WHITE, (64, 39, 18, 9), math.pi, 2 * math.pi, 3)
    pygame.draw.arc(sprite, (245, 93, 101), (66, 44, 15, 7), 0, math.pi, 3)

    # Both arms on the bars.
    left_shoulder = Vector2(80, 50)
    left_elbow = Vector2(103, 55)
    left_hand = Vector2(137, 58)
    right_shoulder = Vector2(108, 48)
    right_elbow = Vector2(125, 49)
    right_hand = Vector2(151, 53)

    pygame.draw.line(sprite, skin_shadow, left_shoulder, left_elbow, 14)
    pygame.draw.line(sprite, skin, left_shoulder - Vector2(1, 2), left_elbow - Vector2(1, 2), 10)
    pygame.draw.line(sprite, skin_shadow, left_elbow, left_hand, 13)
    pygame.draw.line(sprite, skin, left_elbow - Vector2(1, 2), left_hand - Vector2(1, 2), 9)
    pygame.draw.circle(sprite, skin, left_hand, 7)

    pygame.draw.line(sprite, skin_shadow, right_shoulder, right_elbow, 13)
    pygame.draw.line(sprite, skin, right_shoulder - Vector2(1, 2), right_elbow - Vector2(1, 2), 9)
    pygame.draw.line(sprite, skin_shadow, right_elbow, right_hand, 12)
    pygame.draw.line(sprite, skin, right_elbow - Vector2(1, 2), right_hand - Vector2(1, 2), 8)
    pygame.draw.circle(sprite, skin, right_hand, 7)

    # Rotate to track direction.
    angle = -math.degrees(math.atan2(tangent.y, tangent.x))
    rotated = pygame.transform.rotozoom(sprite, angle, 0.61)
    destination = rotated.get_rect(center=(round(position.x), round(position.y)))
    surface.blit(rotated, destination)

    if label:
        label_font = make_font(14, True)
        image = label_font.render(rider.name.upper(), True, WHITE)
        label_rect = image.get_rect(
            midbottom=(round(position.x), round(position.y - 49))
        )
        background = label_rect.inflate(14, 7)
        pygame.draw.rect(surface, INK, background.move(2, 3), border_radius=5)
        pygame.draw.rect(surface, rider.primary, background, border_radius=5)
        pygame.draw.rect(surface, rider.secondary, background, 2, border_radius=5)
        pygame.draw.polygon(
            surface,
            rider.primary,
            [
                (position.x - 5, background.bottom),
                (position.x + 5, background.bottom),
                (position.x, background.bottom + 7),
            ],
        )
        surface.blit(image, label_rect)


def power_bar_fraction(intensity: float) -> float:
    """
    Logarithmic display scale.

    100% FTP sits near the middle of the bar, preserving useful visual
    resolution around normal riding, while still displaying up to 500%.
    """
    intensity = clamp(intensity, 0.0, 5.0)
    return math.log1p(4.0 * intensity) / math.log1p(20.0)


def draw_rider_card(surface, rider, y):
    rect = pygame.Rect(13, y, 202, 105)
    draw_panel(surface, rect, rider.primary)

    draw_text(surface, make_font(18, True), rider.name.upper(), (28, y + 12), rider.secondary)
    draw_text(
        surface,
        make_mono_font(30, True),
        f"{round(rider.power):4d} W",
        (28, y + 33),
        WHITE,
        "topleft",
        True,
    )
    draw_text(surface, make_font(14, True), f"FTP {rider.ftp} W", (28, y + 65), MUTED)
    draw_text(
        surface,
        make_mono_font(14, True),
        f"{rider.intensity * 100:5.0f}%",
        (199, y + 65),
        rider.secondary,
        "topright",
    )

    bar = pygame.Rect(28, y + 82, 171, 13)
    pygame.draw.rect(surface, INK, bar, border_radius=5)

    fill = bar.copy()
    fill.width = max(0, round(bar.width * power_bar_fraction(rider.intensity)))
    if fill.width > 0:
        gradient_rect(surface, fill, rider.secondary, rider.primary)

    pygame.draw.rect(surface, WHITE, bar, 1, border_radius=5)

    ftp_x = bar.x + round(bar.width * power_bar_fraction(1.0))
    pygame.draw.line(surface, WHITE, (ftp_x, bar.y - 3), (ftp_x, bar.bottom + 3), 2)

    draw_text(surface, make_font(10, True), "FTP", (ftp_x, bar.bottom + 1), MUTED, "midtop")


def race_gap(riders: list[Rider]) -> tuple[str, str, tuple[int, int, int]]:
    first, second = riders
    difference = first.distance - second.distance

    if abs(difference) <= TIE_EPSILON_METRES:
        return "DEAD HEAT", "0.0 m 0.00 s", YELLOW

    leader = first if difference > 0 else second
    trailer = second if difference > 0 else first
    gap_metres = abs(difference)
    gap_seconds = gap_metres / max(0.1, trailer.speed_mps)

    return (
        leader.name.upper(),
        f"{gap_metres:.1f} m {gap_seconds:.2f} s",
        leader.secondary,
    )


def draw_hud(surface, riders, elapsed):
    pygame.draw.rect(surface, INK, (0, 0, HUD_W, VH))
    gradient_rect(surface, pygame.Rect(0, 0, HUD_W, 84), (23, 45, 81), (8, 17, 30))
    pygame.draw.line(surface, CYAN, (HUD_W - 2, 0), (HUD_W - 2, VH), 3)

    draw_text(surface, make_font(30, True), "POWER", (19, 12), YELLOW, shadow=True)
    draw_text(surface, make_font(30, True), "TRACK", (114, 37), ORANGE, "center", True)
    draw_text(surface, make_font(12, True), "FRONT-WHEEL 90s TURBO", (114, 67), MUTED, "center")

    draw_rider_card(surface, riders[0], 88)
    draw_rider_card(surface, riders[1], 202)

    leader, gap, leader_colour = race_gap(riders)
    leader_panel = pygame.Rect(13, 316, 202, 77)
    draw_panel(surface, leader_panel, leader_colour)
    draw_text(surface, make_font(13, True), "CURRENT LEADER", (28, 327), MUTED)
    draw_text(surface, make_font(20, True), leader, (28, 345), leader_colour)

    # Separate fixed rows prevent any overlap.
    gap_font = make_mono_font(14, True)
    draw_text(surface, gap_font, "DIST", (28, 370), MUTED, "bottomleft")
    draw_text(surface, gap_font, "TIME", (112, 370), MUTED, "bottomleft")

    if gap.strip().startswith("0.0") or leader == "DEAD HEAT":
        distance_text = "  0.0 m"
        time_text = " 0.00 s"
    else:
        parts = gap.split()
        distance_text = f"{parts[0]:>5} m"
        time_text = f"{parts[2]:>5} s"

    draw_text(surface, gap_font, distance_text, (28, 387), WHITE, "bottomleft")
    draw_text(surface, gap_font, time_text, (112, 387), WHITE, "bottomleft")

    race_panel = pygame.Rect(13, 401, 202, 84)
    draw_panel(surface, race_panel, YELLOW)
    draw_text(surface, make_font(14, True), "RACE CLOCK", (28, 412), YELLOW)

    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    timer_text = f"{minutes:02d}:{seconds:05.2f}"
    timer_font = make_mono_font(24, True)
    draw_text(surface, timer_font, timer_text, (28, 434), CYAN, "topleft")
    draw_text(surface, make_font(17, True), f"LAP 1/{LAPS_TO_WIN}", (28, 465), WHITE)

    draw_text(surface, make_font(11), "ANT+ LIVE    RIDER 1", (18, 496), MUTED)
    draw_text(surface, make_font(11), "ANT+ LIVE    RIDER 2", (18, 510), MUTED)
    draw_text(surface, make_font(10), "SPACE PAUSE · R SETUP · F11 FULLSCREEN", (18, 526), MUTED)


def draw_setup_field(surface, rect, label, value, active, accent, value_font):
    pygame.draw.rect(surface, INK, rect.move(4, 5), border_radius=10)
    pygame.draw.rect(surface, (27, 43, 68) if active else (16, 29, 48), rect, border_radius=10)
    pygame.draw.rect(surface, accent if active else (76, 112, 148), rect, 3 if active else 2, border_radius=10)
    draw_text(surface, make_font(13, True), label, (rect.x + 14, rect.y + 9), MUTED)
    draw_text(surface, value_font, value, (rect.x + 14, rect.y + 32), WHITE)


def ant_device_label(snapshot, channel_index):
    """Show the ANT device ID and current live power."""
    channel = snapshot["channels"][channel_index]
    if not channel["connected"]:
        return f"CHANNEL {channel_index + 1}: SEARCHING..."

    if channel.get("fresh", False):
        signal_note = ""
    elif channel.get("within_dropout_grace", False):
        signal_note = "  •  HOLD"
    elif channel.get("restarting", False):
        signal_note = "  •  SEARCHING AGAIN…"
    elif channel.get("retry_in_seconds", 0.0) > 0.0:
        retry_seconds = max(
            1,
            int(channel["retry_in_seconds"] + 0.999),
        )
        signal_note = f"  •  LOST — RETRY {retry_seconds}s"
    else:
        signal_note = "  •  LOST"

    fec_status = snapshot.get("fec_status", {}).get(
        str(channel["device_id"]),
        "",
    )

    if "RESISTANCE 20%" in fec_status:
        # Tick means the matching FE-C profile was found and OpenANT's
        # resistance command completed without raising an exception.
        fec_note = "  •  ✓ FE-C 20%"
    elif fec_status.startswith("FE-C SETTING"):
        fec_note = "  •  FE-C SETTING…"
    elif fec_status.startswith("FE-C SEARCHING"):
        fec_note = "  •  FE-C SEARCHING…"
    elif fec_status.startswith("FE-C CONTROL FAILED"):
        fec_note = "  •  FE-C FAILED"
    elif fec_status == "FE-C UNAVAILABLE IN OPENANT":
        fec_note = "  •  NO FE-C SUPPORT"
    else:
        fec_note = ""

    return (
        f"ANT ID {channel['device_id']}  •  "
        f"{channel['power']} W  •  RX {channel_index + 1}"
        f"{signal_note}{fec_note}"
    )


def draw_device_field(
    surface,
    rect,
    rider_index,
    channel_index,
    snapshot,
    active,
    accent,
):
    pygame.draw.rect(surface, INK, rect.move(4, 5), border_radius=10)
    pygame.draw.rect(
        surface,
        (28, 47, 69) if active else (15, 29, 46),
        rect,
        border_radius=10,
    )
    pygame.draw.rect(
        surface,
        accent if active else PANEL_EDGE,
        rect,
        3 if active else 2,
        border_radius=10,
    )

    draw_text(
        surface,
        make_font(12, True),
        "ANT DEVICE — REFRESH / NEXT",
        (rect.x + 12, rect.y + 8),
        MUTED,
    )
    draw_text(
        surface,
        make_font(13, True),
        ant_device_label(snapshot, channel_index)[:49],
        (rect.x + 12, rect.y + 31),
        WHITE,
    )

    # Small cycling arrow indicates that this is a selector.
    draw_text(
        surface,
        make_font(22, True),
        "↻",
        (rect.right - 22, rect.centery),
        accent,
        "center",
    )


def available_ant_channels(snapshot):
    """
    Return one physical channel for each unique connected ANT device ID.

    Wildcard ANT channels can occasionally discover the same transmitter more
    than once. Deduplicating by device ID prevents duplicate selector entries.
    """
    unique_channels = []
    seen_device_ids = set()

    for channel_index, channel in enumerate(snapshot["channels"]):
        if not channel.get("connected"):
            continue

        device_id = int(channel.get("device_id", 0) or 0)
        if device_id <= 0 or device_id in seen_device_ids:
            continue

        seen_device_ids.add(device_id)
        unique_channels.append(channel_index)

    return unique_channels


def cycle_rider_ant_device(assignments, rider_index, snapshot):
    """
    Refresh the discovered-device list and select the next available device.

    The other rider's selected ANT device is skipped. If the next device is
    already assigned to the other rider, the assignments swap.
    """
    available = available_ant_channels(snapshot)
    if not available:
        return

    other_rider = 1 - rider_index
    current_channel = assignments[rider_index]
    other_channel = assignments[other_rider]

    if current_channel in available:
        start_position = available.index(current_channel)
    else:
        start_position = -1

    for offset in range(1, len(available) + 1):
        candidate = available[
            (start_position + offset) % len(available)
        ]

        if candidate == other_channel:
            # With only two unique devices, swapping is more useful than
            # refusing the selection.
            if len(available) == 2:
                assignments[other_rider] = current_channel
                assignments[rider_index] = candidate
                return
            continue

        assignments[rider_index] = candidate
        return

    # Only one unique device is available. Keep it on its existing rider;
    # otherwise assign it to the selected rider and clear the duplicate by
    # leaving the other rider on its previous (possibly searching) channel.
    sole_channel = available[0]
    if sole_channel != other_channel:
        assignments[rider_index] = sole_channel


def normalise_ant_assignments(assignments, snapshot):
    """
    Move invalid/default assignments onto distinct discovered devices.

    This lets devices found on channels 3–8 become selectable without requiring
    them to be among the first two receiver channels.
    """
    available = available_ant_channels(snapshot)
    if not available:
        return

    used = set()
    for rider_index in range(2):
        selected = assignments[rider_index]
        if selected in available and selected not in used:
            used.add(selected)
            continue

        replacement = next(
            (channel for channel in available if channel not in used),
            available[0],
        )
        assignments[rider_index] = replacement
        used.add(replacement)


def draw_setup_screen(
    surface,
    riders,
    active_field,
    setup_rects,
    ant_manager,
    assignments,
):
    shade = pygame.Surface((VW, VH), pygame.SRCALPHA)
    shade.fill((3, 7, 14, 215))
    surface.blit(shade, (0, 0))

    panel_rect = pygame.Rect(270, 53, 650, 452)
    pygame.draw.rect(surface, INK, panel_rect.move(8, 10), border_radius=18)
    gradient_rect(surface, panel_rect, (27, 48, 80), (10, 22, 39))
    pygame.draw.rect(surface, CYAN, panel_rect, 3, border_radius=18)

    draw_text(
        surface,
        make_font(38, True),
        "RACE SETUP",
        (panel_rect.centerx, 69),
        YELLOW,
        "midtop",
        True,
    )
    draw_text(
        surface,
        make_font(15, True),
        "Edit each rider, then click their ANT device field to choose a trainer.",
        (panel_rect.centerx, 113),
        WHITE,
        "midtop",
    )

    snapshot = ant_manager.snapshot()
    normalise_ant_assignments(assignments, snapshot)
    status = snapshot["error"] or snapshot["status"]
    unique_device_ids = {
        int(channel.get("device_id", 0) or 0)
        for channel in snapshot["channels"]
        if channel.get("connected")
        and int(channel.get("device_id", 0) or 0) > 0
    }
    if not snapshot["error"]:
        status = (
            f"{status} — {len(unique_device_ids)} UNIQUE DEVICE"
            f"{'' if len(unique_device_ids) == 1 else 'S'} FOUND"
        )
    status_colour = RED if snapshot["error"] else (
        GREEN if snapshot["running"] else YELLOW
    )
    draw_text(
        surface,
        make_font(12, True),
        status[:76],
        (panel_rect.centerx, 139),
        status_colour,
        "midtop",
    )

    headings_y = 169
    draw_text(surface, make_font(13, True), "NAME", (298, headings_y), MUTED)
    draw_text(surface, make_font(13, True), "FTP", (488, headings_y), MUTED)
    draw_text(surface, make_font(13, True), "ANT DEVICE", (596, headings_y), MUTED)

    for rider_index, rider in enumerate(riders):
        row_y = 188 + rider_index * 105
        accent = rider.secondary

        draw_text(
            surface,
            make_font(18, True),
            f"RIDER {rider_index + 1}",
            (298, row_y - 17),
            accent,
        )

        draw_setup_field(
            surface,
            setup_rects[f"r{rider_index + 1}_name"],
            "",
            rider.name,
            active_field == f"r{rider_index + 1}_name",
            accent,
            make_font(22, True),
        )
        draw_setup_field(
            surface,
            setup_rects[f"r{rider_index + 1}_ftp"],
            "",
            str(rider.ftp),
            active_field == f"r{rider_index + 1}_ftp",
            accent,
            make_mono_font(22, True),
        )
        draw_device_field(
            surface,
            setup_rects[f"r{rider_index + 1}_device"],
            rider_index,
            assignments[rider_index],
            snapshot,
            active_field == f"r{rider_index + 1}_device",
            accent,
        )

    start_rect = setup_rects["start"]
    pygame.draw.rect(surface, INK, start_rect.move(5, 6), border_radius=13)
    pygame.draw.rect(surface, GREEN, start_rect, border_radius=13)
    pygame.draw.rect(surface, WHITE, start_rect, 3, border_radius=13)
    draw_text(
        surface,
        make_font(30, True),
        "START RACE",
        start_rect.center,
        INK,
        "center",
        True,
    )

    draw_text(
        surface,
        make_font(12),
        "Clicking Name or FTP selects the whole value: just start typing.",
        (panel_rect.centerx, 478),
        MUTED,
        "center",
    )


PREFERENCES_FILE = (
    Path(__file__).resolve().parent / "rider_preferences.json"
)


def load_rider_preferences():
    defaults = [
        {"name": "Blue Bolt", "ftp": 250},
        {"name": "Red Rocket", "ftp": 200},
    ]
    try:
        data = json.loads(
            PREFERENCES_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return defaults

    riders = data.get("riders") if isinstance(data, dict) else None
    if not isinstance(riders, list) or len(riders) < 2:
        return defaults

    result = []
    for index in range(2):
        item = riders[index] if isinstance(riders[index], dict) else {}
        name = str(item.get("name", defaults[index]["name"])).strip()[:14]
        try:
            ftp = int(item.get("ftp", defaults[index]["ftp"]))
        except (TypeError, ValueError):
            ftp = defaults[index]["ftp"]
        result.append({
            "name": name or defaults[index]["name"],
            "ftp": max(1, min(2000, ftp)),
        })
    return result


def save_rider_preferences(riders):
    payload = {
        "riders": [
            {"name": rider.name, "ftp": int(rider.ftp)}
            for rider in riders
        ]
    }
    try:
        PREFERENCES_FILE.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def find_previous_ftp_from_logs(name):
    """Return the newest logged FTP for an exact rider-name match."""
    wanted = name.strip().casefold()
    if not wanted:
        return None

    log_directory = Path(__file__).resolve().parent / "race_logs"
    if not log_directory.exists():
        return None

    files = sorted(
        log_directory.glob("power_track_results_*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for path in files:
        try:
            lines = path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            continue

        for index, line in enumerate(lines):
            if not line.startswith("Name:"):
                continue
            logged_name = line.partition(":")[2].strip().casefold()
            if logged_name != wanted:
                continue

            for following in lines[index + 1:index + 5]:
                match = re.match(r"FTP:\s*(\d+)\s*W", following)
                if match:
                    return max(1, min(2000, int(match.group(1))))
    return None


def reset_rider_race_state(riders):
    for rider in riders:
        rider.distance = 0.0
        rider.speed_mps = 0.0
        rider.finished = False
        rider.finish_time = None
        rider.lap = 1
        rider.power_samples = []
        rider.best_60s_power = 0.0
        rider.total_power_integral = 0.0
        rider.power_sample_duration = 0.0


def setup_tab_order():
    return (
        "r1_name",
        "r1_ftp",
        "r1_device",
        "r2_name",
        "r2_ftp",
        "r2_device",
        "start",
    )


def next_setup_field(current):
    order = setup_tab_order()
    if current not in order:
        return order[0]
    return order[(order.index(current) + 1) % len(order)]


def draw_exit_confirmation(surface):
    shade = pygame.Surface((VW, VH), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 180))
    surface.blit(shade, (0, 0))

    panel = pygame.Rect(385, 190, 420, 190)
    pygame.draw.rect(surface, INK, panel.move(7, 8), border_radius=16)
    pygame.draw.rect(surface, (24, 39, 65), panel, border_radius=16)
    pygame.draw.rect(surface, YELLOW, panel, 3, border_radius=16)

    draw_text(
        surface,
        make_font(31, True),
        "EXIT POWER TRACK?",
        (panel.centerx, 222),
        YELLOW,
        "center",
        True,
    )
    draw_text(
        surface,
        make_font(15, True),
        "Press Y or Enter to exit — N or Esc to stay",
        (panel.centerx, 274),
        WHITE,
        "center",
    )

    yes_rect = pygame.Rect(445, 309, 130, 45)
    no_rect = pygame.Rect(615, 309, 130, 45)
    for rect, label, colour in (
        (yes_rect, "YES", RED),
        (no_rect, "NO", GREEN),
    ):
        pygame.draw.rect(surface, colour, rect, border_radius=9)
        pygame.draw.rect(surface, WHITE, rect, 2, border_radius=9)
        draw_text(
            surface,
            make_font(20, True),
            label,
            rect.center,
            WHITE,
            "center",
        )
    return yes_rect, no_rect


def apply_setup_text(riders, field_name, text):
    if not field_name:
        return
    rider = riders[0 if field_name.startswith("r1") else 1]
    if field_name.endswith("name"):
        cleaned = text.strip()[:14]
        if cleaned:
            name_changed = cleaned.casefold() != rider.name.casefold()
            rider.name = cleaned
            if name_changed:
                previous_ftp = find_previous_ftp_from_logs(cleaned)
                if previous_ftp is not None:
                    rider.ftp = previous_ftp
                    rider.power = previous_ftp
    else:
        try:
            ftp = int(text)
        except ValueError:
            return
        rider.ftp = max(1, min(2000, ftp))
        rider.power = rider.ftp

    save_rider_preferences(riders)


def new_riders():
    saved = load_rider_preferences()
    return [
        Rider(
            saved[0]["name"],
            saved[0]["ftp"],
            saved[0]["ftp"],
            BLUE,
            CYAN,
            -LANE_OFFSET,
            distance=0.0,
            skin=(244, 185, 137),
            hair=(34, 29, 27),
        ),
        Rider(
            saved[1]["name"],
            saved[1]["ftp"],
            saved[1]["ftp"],
            RED,
            YELLOW,
            LANE_OFFSET,
            distance=0.0,
            skin=(190, 126, 84),
            hair=(55, 34, 24),
        ),
    ]


def draw_velodrome_details(surface):
    """Decorative infield and trackside details."""
    # Infield mowing stripes.
    infield = pygame.Rect(426, 238, 298, 178)
    pygame.draw.ellipse(surface, (36, 137, 67), infield)

    for index, y in enumerate(range(infield.y + 16, infield.bottom - 12, 16)):
        colour = (53, 157, 76) if index % 2 == 0 else (43, 146, 70)
        pygame.draw.line(
            surface,
            colour,
            (infield.x + 30, y),
            (infield.right - 30, y),
            8,
        )

    # Centre logo.
    pygame.draw.ellipse(surface, (10, 20, 35), (510, 282, 132, 82))
    pygame.draw.ellipse(surface, PURPLE, (518, 290, 116, 66), 4)
    pygame.draw.ellipse(surface, CYAN, (526, 298, 100, 50), 3)
    draw_text(surface, make_font(30, True), "PT", (576, 301), YELLOW, "midtop", True)
    draw_text(surface, make_font(12, True), "TURBO CUP", (576, 336), WHITE, "midtop")

    # Sponsor boards.
    sponsors = [
        (450, 377, 72, 23, BLUE, "NOVA"),
        (532, 377, 72, 23, RED, "TURBO"),
        (614, 377, 72, 23, PURPLE, "VOLT"),
    ]
    for x, y, width, height, colour, label in sponsors:
        pygame.draw.rect(surface, INK, (x + 3, y + 3, width, height), border_radius=4)
        pygame.draw.rect(surface, colour, (x, y, width, height), border_radius=4)
        pygame.draw.rect(surface, WHITE, (x, y, width, height), 2, border_radius=4)
        draw_text(
            surface,
            make_font(13, True),
            label,
            (x + width // 2, y + height // 2),
            WHITE,
            "center",
        )

    # Floodlight towers.
    for x, y in [(278, 205), (872, 205), (278, 458), (872, 458)]:
        pygame.draw.line(surface, (61, 72, 86), (x, y), (x, y - 55), 6)
        pygame.draw.rect(surface, (23, 31, 44), (x - 19, y - 66, 38, 13), border_radius=3)
        for lamp_x in (x - 13, x - 4, x + 5, x + 14):
            pygame.draw.circle(surface, (255, 240, 146), (lamp_x, y - 60), 4)


def build_scene(track):
    surface = pygame.Surface((VW, VH))
    draw_background(surface)
    draw_scenery(surface)
    draw_velodrome_details(surface)
    track.draw(surface)
    draw_grandstand(surface)
    draw_title(surface)
    return surface


def winner_result(
    riders: list[Rider],
) -> tuple[str, tuple[int, int, int], str] | None:
    """
    Return a result as soon as the first rider finishes.

    A rider who remains at zero watts is valid and is shown as DNF in the
    summary; no calculation divides by that rider's speed.
    """
    finished = [
        rider
        for rider in riders
        if rider.finished and rider.finish_time is not None
    ]
    if not finished:
        return None

    if len(finished) == 2:
        first_time = riders[0].finish_time
        second_time = riders[1].finish_time
        if (
            first_time is not None
            and second_time is not None
            and abs(first_time - second_time) <= TIE_EPSILON_SECONDS
        ):
            return (
                "DEAD HEAT!",
                YELLOW,
                f"{max(first_time, second_time):.2f} SECONDS",
            )

    winner = min(
        finished,
        key=lambda rider: (
            rider.finish_time
            if rider.finish_time is not None
            else float("inf")
        ),
    )
    finish_time = winner.finish_time if winner.finish_time is not None else 0.0
    return (
        f"{winner.name.upper()} WINS!",
        winner.secondary,
        f"{finish_time:.2f} SECONDS",
    )


def draw_power_summary(surface, riders, winner_text, finish_time):
    """Draw post-race result and power summary."""
    shade = pygame.Surface((VW, VH), pygame.SRCALPHA)
    shade.fill((2, 5, 12, 205))
    surface.blit(shade, (0, 0))

    panel = pygame.Rect(300, 58, 594, 454)
    pygame.draw.rect(surface, INK, panel.move(9, 10), border_radius=18)
    gradient_rect(surface, panel, (30, 47, 78), (9, 20, 37))
    pygame.draw.rect(surface, YELLOW, panel, 3, border_radius=18)

    draw_text(
        surface,
        make_font(38, True),
        winner_text,
        (panel.centerx, 79),
        YELLOW,
        "midtop",
        True,
    )
    draw_text(
        surface,
        make_mono_font(22, True),
        f"FINISH  {finish_time}",
        (panel.centerx, 126),
        CYAN,
        "midtop",
    )

    headings_y = 177
    draw_text(surface, make_font(14, True), "RIDER", (334, headings_y), MUTED)
    draw_text(surface, make_font(14, True), "AVG POWER", (500, headings_y), MUTED)
    draw_text(surface, make_font(14, True), "BEST 1 MIN", (635, headings_y), MUTED)
    draw_text(surface, make_font(14, True), "SUGGESTED FTP", (765, headings_y), MUTED)

    for index, rider in enumerate(riders):
        row_y = 208 + index * 91
        accent = rider.secondary
        row = pygame.Rect(326, row_y, 542, 72)
        pygame.draw.rect(surface, (10, 17, 29), row, border_radius=10)
        pygame.draw.rect(surface, accent, row, 2, border_radius=10)

        draw_text(
            surface,
            make_font(20, True),
            rider.name,
            (342, row_y + 12),
            accent,
        )
        draw_text(
            surface,
            make_mono_font(19, True),
            f"{rider.average_power:4.0f} W",
            (513, row_y + 29),
            WHITE,
            "center",
        )
        draw_text(
            surface,
            make_mono_font(19, True),
            f"{rider.best_60s_power:4.0f} W",
            (672, row_y + 29),
            WHITE,
            "center",
        )
        draw_text(
            surface,
            make_mono_font(19, True),
            f"{rider.suggested_ftp:4.0f} W",
            (817, row_y + 29),
            YELLOW,
            "center",
        )

        finish_status = (
            f"FINISHED {rider.finish_time:.2f}s"
            if rider.finish_time is not None
            else "DID NOT FINISH"
        )
        draw_text(
            surface,
            make_font(11, True),
            finish_status,
            (342, row_y + 48),
            MUTED,
        )

    draw_text(
        surface,
        make_font(14),
        "Suggested FTP = power figure shown × 0.75",
        (panel.centerx, 414),
        WHITE,
        "center",
    )
    draw_text(
        surface,
        make_font(14, True),
        "PRESS R TO RETURN TO SETUP",
        (panel.centerx, 467),
        CYAN,
        "center",
    )


def write_final_results(riders, elapsed, winner_text, assignments):
    """Write one timestamped final-results text file after the race."""
    log_directory = Path(__file__).resolve().parent / "race_logs"
    log_directory.mkdir(parents=True, exist_ok=True)

    finished_at = datetime.now()
    timestamp = finished_at.strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_directory / f"power_track_results_{timestamp}.txt"

    lines = [
        "POWER TRACK FINAL RESULTS",
        f"Timestamp: {finished_at.isoformat(timespec='seconds')}",
        f"Result: {winner_text}",
        f"Race time: {elapsed:.2f} seconds",
        "",
    ]

    for index, rider in enumerate(riders):
        finish_text = (
            f"{rider.finish_time:.2f} seconds"
            if rider.finish_time is not None
            else "DID NOT FINISH"
        )
        power_period = (
            f"{rider.power_sample_duration:.1f}-second average"
            if rider.power_sample_duration < 60.0
            else "best rolling 60-second average"
        )

        lines.extend([
            f"Rider {index + 1}",
            f"Name: {rider.name}",
            f"FTP: {rider.ftp} W",
            f"ANT channel: {assignments[index] + 1}",
            f"Finish: {finish_text}",
            f"Average race power: {rider.average_power:.1f} W",
            f"One-minute power figure: {rider.best_60s_power:.1f} W",
            f"Power period used: {power_period}",
            f"Suggested FTP: {rider.suggested_ftp:.1f} W",
            "",
        ])

    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def main():
    pygame.init()
    pygame.display.set_caption("POWER TRACK: ANT AUTO RESEARCH")
    screen = pygame.display.set_mode((WINDOW_W, WINDOW_H), pygame.RESIZABLE)
    virtual = pygame.Surface((VW, VH))
    clock = pygame.time.Clock()

    track = Track()
    background = build_scene(track)
    riders = new_riders()

    start_ms = pygame.time.get_ticks()
    paused_total = 0
    pause_start = 0
    paused = False
    fullscreen = False
    race_finish_elapsed = None
    race_started = False
    countdown_started_at = None
    countdown_seconds = 5.0
    active_field = None
    input_buffer = ""
    replace_on_type = False
    setup_rects = {
        "r1_name": pygame.Rect(298, 188, 178, 62),
        "r1_ftp": pygame.Rect(488, 188, 92, 62),
        "r1_device": pygame.Rect(592, 188, 296, 62),
        "r2_name": pygame.Rect(298, 293, 178, 62),
        "r2_ftp": pygame.Rect(488, 293, 92, 62),
        "r2_device": pygame.Rect(592, 293, 296, 62),
        "start": pygame.Rect(476, 397, 238, 58),
    }
    # Logical rider -> physical ANT receiver channel.
    assignments = [0, 1]

    ant_manager = AntPowerManager(channel_count=6, fec_channel_count=2)
    ant_manager.start()

    final_results_logged = False
    exit_confirm = False
    exit_yes_rect = pygame.Rect(445, 309, 130, 45)
    exit_no_rect = pygame.Rect(615, 309, 130, 45)
    last_selected_device_ids = [None, None]
    running = True

    while running:
        dt = min(clock.tick(FPS) / 1000.0, 0.05)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                exit_confirm = True

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                sw, sh = screen.get_size()
                scale = min(sw / VW, sh / VH)
                ox = (sw - VW * scale) / 2
                oy = (sh - VH * scale) / 2
                virtual_mouse = ((event.pos[0] - ox) / max(scale, 0.001),
                                 (event.pos[1] - oy) / max(scale, 0.001))

                if exit_confirm:
                    if exit_yes_rect.collidepoint(virtual_mouse):
                        running = False
                    elif exit_no_rect.collidepoint(virtual_mouse):
                        exit_confirm = False
                    continue

                if not race_started and countdown_started_at is None:
                    clicked_field = None
                    for field_name in (
                        "r1_name",
                        "r1_ftp",
                        "r2_name",
                        "r2_ftp",
                    ):
                        if setup_rects[field_name].collidepoint(virtual_mouse):
                            clicked_field = field_name
                            break

                    clicked_device = None
                    for rider_index in range(2):
                        field_name = f"r{rider_index + 1}_device"
                        if setup_rects[field_name].collidepoint(virtual_mouse):
                            clicked_device = rider_index
                            break

                    if clicked_device is not None:
                        if active_field and not active_field.endswith("device"):
                            apply_setup_text(
                                riders,
                                active_field,
                                input_buffer,
                            )

                        snapshot = ant_manager.snapshot()
                        normalise_ant_assignments(
                            assignments,
                            snapshot,
                        )
                        cycle_rider_ant_device(
                            assignments,
                            clicked_device,
                            snapshot,
                        )
                        active_field = f"r{clicked_device + 1}_device"
                        input_buffer = ""
                        replace_on_type = False

                    elif clicked_field:
                        if active_field and active_field != clicked_field:
                            apply_setup_text(
                                riders,
                                active_field,
                                input_buffer,
                            )

                        active_field = clicked_field
                        rider = riders[
                            0 if clicked_field.startswith("r1") else 1
                        ]
                        input_buffer = (
                            rider.name
                            if clicked_field.endswith("name")
                            else str(rider.ftp)
                        )
                        replace_on_type = True

                    elif setup_rects["start"].collidepoint(virtual_mouse):
                        if active_field and not active_field.endswith("device"):
                            apply_setup_text(
                                riders,
                                active_field,
                                input_buffer,
                            )
                        active_field = None
                        input_buffer = ""
                        replace_on_type = False
                        replace_on_type = False

                        save_rider_preferences(riders)
                        reset_rider_race_state(riders)

                        paused_total = 0
                        race_finish_elapsed = None
                        paused = False
                        race_started = False
                        final_results_logged = False
                        countdown_started_at = monotonic()

                    else:
                        if active_field and not active_field.endswith("device"):
                            apply_setup_text(
                                riders,
                                active_field,
                                input_buffer,
                            )
                        active_field = None
                        input_buffer = ""
                        replace_on_type = False
                        replace_on_type = False

            elif event.type == pygame.KEYDOWN:
                if exit_confirm:
                    if event.key in (pygame.K_y, pygame.K_RETURN):
                        running = False
                    elif event.key in (pygame.K_n, pygame.K_ESCAPE):
                        exit_confirm = False
                    continue

                if event.key == pygame.K_ESCAPE:
                    if (
                        active_field
                        and not active_field.endswith("device")
                    ):
                        apply_setup_text(
                            riders,
                            active_field,
                            input_buffer,
                        )
                    active_field = None
                    input_buffer = ""
                    replace_on_type = False
                    exit_confirm = True

                elif (
                    not race_started
                    and active_field
                    and not active_field.endswith("device")
                ):
                    if event.key == pygame.K_TAB:
                        apply_setup_text(
                            riders,
                            active_field,
                            input_buffer,
                        )
                        active_field = next_setup_field(active_field)
                        input_buffer = ""
                        replace_on_type = False

                        if active_field.endswith("name"):
                            rider = riders[
                                0 if active_field.startswith("r1") else 1
                            ]
                            input_buffer = rider.name
                            replace_on_type = True
                        elif active_field.endswith("ftp"):
                            rider = riders[
                                0 if active_field.startswith("r1") else 1
                            ]
                            input_buffer = str(rider.ftp)
                            replace_on_type = True

                    elif event.key == pygame.K_RETURN:
                        apply_setup_text(
                            riders,
                            active_field,
                            input_buffer,
                        )
                        active_field = None
                        input_buffer = ""
                        replace_on_type = False

                    elif event.key == pygame.K_BACKSPACE:
                        if replace_on_type:
                            input_buffer = ""
                            replace_on_type = False
                        else:
                            input_buffer = input_buffer[:-1]

                    else:
                        character = event.unicode
                        if active_field.endswith("name"):
                            valid = (
                                character
                                and (
                                    character.isalnum()
                                    or character in " -_'"
                                )
                            )
                            if valid:
                                if replace_on_type:
                                    input_buffer = ""
                                    replace_on_type = False
                                if len(input_buffer) < 14:
                                    input_buffer += character
                        else:
                            if character.isdigit():
                                if replace_on_type:
                                    input_buffer = ""
                                    replace_on_type = False
                                if len(input_buffer) < 4:
                                    input_buffer += character

                elif (
                    not race_started
                    and countdown_started_at is None
                    and event.key == pygame.K_TAB
                ):
                    active_field = next_setup_field(active_field)
                    input_buffer = ""
                    replace_on_type = False

                    if active_field.endswith("name"):
                        rider = riders[
                            0 if active_field.startswith("r1") else 1
                        ]
                        input_buffer = rider.name
                        replace_on_type = True
                    elif active_field.endswith("ftp"):
                        rider = riders[
                            0 if active_field.startswith("r1") else 1
                        ]
                        input_buffer = str(rider.ftp)
                        replace_on_type = True

                elif race_started:
                    if event.key == pygame.K_SPACE:
                        paused = not paused
                        if paused:
                            pause_start = pygame.time.get_ticks()
                        else:
                            paused_total += pygame.time.get_ticks() - pause_start
                    elif event.key == pygame.K_r:
                        save_rider_preferences(riders)
                        reset_rider_race_state(riders)
                        race_started = False
                        countdown_started_at = None
                        active_field = None
                        input_buffer = ""
                        replace_on_type = False
                        paused_total = 0
                        paused = False
                        race_finish_elapsed = None
                        final_results_logged = False
                    elif event.key == pygame.K_F11:
                        fullscreen = not fullscreen
                        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN) if fullscreen else \
                                 pygame.display.set_mode((WINDOW_W, WINDOW_H), pygame.RESIZABLE)
                    elif event.key == pygame.K_a:
                        riders[0].power = riders[0].ftp
                    elif event.key == pygame.K_LEFT:
                        riders[1].power = riders[1].ftp

        keys = pygame.key.get_pressed()

        ant_snapshot = ant_manager.snapshot()

        for rider_index in range(2):
            selected_channel = assignments[rider_index]
            if not 0 <= selected_channel < len(ant_snapshot["channels"]):
                continue
            selected_device_id = int(
                ant_snapshot["channels"][selected_channel].get(
                    "device_id",
                    0,
                )
                or 0
            )
            if (
                selected_device_id > 0
                and selected_device_id
                != last_selected_device_ids[rider_index]
            ):
                ant_manager.set_basic_resistance_for_device(
                    selected_device_id,
                    20.0,
                )
                last_selected_device_ids[rider_index] = selected_device_id

        for rider_index, rider in enumerate(riders):
            physical_channel = assignments[rider_index]
            channel = ant_snapshot["channels"][physical_channel]
            rider.power = (
                float(channel["power"])
                if channel.get(
                    "within_dropout_grace",
                    channel["fresh"],
                )
                else 0.0
            )

        countdown_value = None
        if countdown_started_at is not None and not race_started:
            remaining = countdown_seconds - (monotonic() - countdown_started_at)
            if remaining <= 0:
                countdown_started_at = None
                race_started = True
                start_ms = pygame.time.get_ticks()
                paused_total = 0
                race_finish_elapsed = None
                for rider in riders:
                    rider.distance = 0.0
                    rider.speed_mps = 0.0
                    rider.finished = False
                    rider.finish_time = None
                    rider.lap = 1

                final_results_logged = False
            else:
                countdown_value = max(1, math.ceil(remaining))

        now = pygame.time.get_ticks()
        effective_now = pause_start if paused else now
        live_elapsed = max(0.0, (effective_now - start_ms - paused_total) / 1000.0) if race_started else 0.0

        if race_started and not paused and race_finish_elapsed is None:
            for rider in riders:
                rider.record_power(live_elapsed, dt)
                rider.update(dt, live_elapsed, track.length)
            finished_times = [r.finish_time for r in riders if r.finish_time is not None]
            if finished_times:
                race_finish_elapsed = min(finished_times)

        elapsed = race_finish_elapsed if race_finish_elapsed is not None else live_elapsed

        virtual.blit(background, (0, 0))
        draw_hud(virtual, riders, elapsed)

        draw_items = []
        for rider in riders:
            position, tangent = track.sample(rider.distance, rider.lane)
            draw_items.append((position.y, rider, position, tangent))
        for _, rider, position, tangent in sorted(draw_items):
            draw_rider_sprite(virtual, position, tangent, rider)

        if paused:
            shade = pygame.Surface((VW, VH), pygame.SRCALPHA)
            shade.fill((0, 0, 0, 145))
            virtual.blit(shade, (0, 0))
            draw_text(virtual, make_font(67, True), "PAUSED", (594, 260), WHITE, "center", True)

        if not race_started and countdown_started_at is None:
            if active_field and not active_field.endswith("device"):
                rider = riders[0 if active_field.startswith("r1") else 1]
                original_name, original_ftp = rider.name, rider.ftp
                if active_field.endswith("name"):
                    rider.name = input_buffer
                else:
                    try:
                        rider.ftp = (
                            int(input_buffer)
                            if input_buffer
                            else 0
                        )
                    except ValueError:
                        rider.ftp = 0

                draw_setup_screen(
                    virtual,
                    riders,
                    active_field,
                    setup_rects,
                    ant_manager,
                    assignments,
                )
                rider.name, rider.ftp = original_name, original_ftp
            else:
                draw_setup_screen(
                    virtual,
                    riders,
                    active_field,
                    setup_rects,
                    ant_manager,
                    assignments,
                )

        if countdown_value is not None:
            shade = pygame.Surface((VW, VH), pygame.SRCALPHA)
            shade.fill((2, 5, 12, 150))
            virtual.blit(shade, (0, 0))
            centre = (594, 270)
            pygame.draw.circle(virtual, INK, centre, 94)
            pygame.draw.circle(virtual, YELLOW, centre, 86)
            pygame.draw.circle(virtual, ORANGE, centre, 76)
            draw_text(virtual, make_font(96, True), str(countdown_value), centre, WHITE, "center", True)
            draw_text(virtual, make_font(25, True), "GET READY", (594, 383), CYAN, "center", True)

        result = winner_result(riders) if race_started else None
        if result:
            winner_text, winner_colour, time_text = result

            if not final_results_logged:
                write_final_results(
                    riders,
                    elapsed,
                    winner_text,
                    assignments,
                )
                final_results_logged = True

            draw_power_summary(
                virtual,
                riders,
                winner_text,
                time_text,
            )

        if exit_confirm:
            exit_yes_rect, exit_no_rect = draw_exit_confirmation(virtual)

        screen.fill(INK)
        screen_width, screen_height = screen.get_size()
        scale = min(screen_width / VW, screen_height / VH)
        output_width = max(1, round(VW * scale))
        output_height = max(1, round(VH * scale))
        scaled = pygame.transform.smoothscale(
            virtual,
            (output_width, output_height),
        )
        screen.blit(
            scaled,
            (
                (screen_width - output_width) // 2,
                (screen_height - output_height) // 2,
            ),
        )
        pygame.display.flip()

    save_rider_preferences(riders)
    ant_manager.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
