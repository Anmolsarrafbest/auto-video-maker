"""CPU-rendered science inserts for the Shorts renderer."""

import math

from manim import (Arrow, Circle, Create, Dot, Ellipse, FadeIn, GrowFromEdge, LEFT, Line,
                   MoveAlongPath, Rectangle, Scene, Text, UP, VGroup, linear, tempconfig)


class RainbowDrop(Scene):
    def construct(self):
        drop = Circle(radius=2.35, color="#7FE4DB", stroke_width=4,
                      fill_color="#20534F", fill_opacity=.65)
        title = Text("INSIDE A RAINDROP", color="#DAFFF1", font_size=30).move_to((0, 3, 0))
        entry = (-1.88, 1.41, 0)
        bounce = (2.32, .37, 0)
        exit_point = (-.68, -2.25, 0)
        incoming = Line((-5.8, 1.41, 0), entry, color="#FFF7D4", stroke_width=7)
        through = Line(entry, bounce, color="#FFF7D4", stroke_width=6)
        reflected = Line(bounce, exit_point, color="#FFF7D4", stroke_width=6)
        spectrum = VGroup(*(Line(exit_point, (-5.4, -2.36 - i * .18, 0),
                                 color=color, stroke_width=5)
                            for i, color in enumerate(("#FF695B", "#FFD067", "#76E6A5", "#7E9BFF", "#C69BFF"))))
        self.add(drop, title)
        self.play(Create(incoming), run_time=.65)
        self.play(Create(through), run_time=.75)
        self.play(Create(reflected), run_time=.8)
        self.play(*(Create(ray) for ray in spectrum), run_time=.85)
        self.play(FadeIn(Text("ONE INTERNAL REFLECTION", color="#DAFFF1", font_size=22)
                         .move_to((2.8, -3, 0))), run_time=.35)
        self.wait(.6)


class VenusPeriods(Scene):
    def construct(self):
        title = Text("VENUS: SPIN VS. ORBIT", color="#FFF0D2", font_size=32).move_to((0, 2.8, 0))
        self.add(title)
        for days, name, color, y in ((243, "One spin", "#FFBE86", 1.2),
                                     (225, "One orbit", "#83D7DF", -1)):
            width = 8 * days / 243
            bar = Rectangle(width=width, height=.55, stroke_width=0,
                            fill_color=color, fill_opacity=1).move_to((-4.5 + width / 2, y, 0))
            label = Text(f"{name}  ·  {days} Earth days", color=color, font_size=27).next_to(bar, UP)
            self.play(GrowFromEdge(bar, LEFT), FadeIn(label), run_time=1.1)
        self.play(FadeIn(Text("SIDEREAL ROTATION", color="#FFF0D2", font_size=22)
                         .move_to((0, -2.7, 0))), run_time=.5)
        self.wait(1.3)


class MercuryDay(Scene):
    def construct(self):
        orbit = Ellipse(width=8.8, height=3.9, color="#7285A6")
        sun = Circle(.62, color="#FFB75D", fill_opacity=1).shift(LEFT * 1.2)
        planet = Dot(orbit.get_start(), radius=.16, color="#FFE2AC")
        title = Text("MERCURY’S SOLAR DAY", font_size=32).move_to((0, 3.2, 0))
        self.add(title, sun, planet)
        self.play(Create(orbit), run_time=.4)
        for number, days in ((1, 88), (2, 176)):
            label = Text(f"{number} orbit{'s' if number == 2 else ''} · {days} Earth days",
                         font_size=27, color="#FFE2AC").move_to((0, -2.8, 0))
            self.play(MoveAlongPath(planet, orbit, rate_func=linear), FadeIn(label), run_time=1.35)
            if number == 1:
                self.remove(label)
        self.wait(.7)


class LunarEclipse(Scene):
    def construct(self):
        title = Text("LIGHT THROUGH EARTH’S ATMOSPHERE", font_size=27).move_to((0, 3, 0))
        sun = Circle(.82, color="#FFBE67", fill_opacity=1).move_to((-4.7, 0, 0))
        earth = Circle(1, color="#70B8F2", fill_color="#254986", fill_opacity=1)
        moon = Circle(.52, color="#AFBCCF", fill_opacity=1).move_to((4.5, 0, 0))
        self.add(title, sun, earth, moon)
        rays = VGroup(*(Line((-3.8, y * 1.35, 0), (-.35, y, 0), color="#FFD281")
                        for y in (-1, 1)))
        self.play(Create(rays), run_time=.85)
        refracted = VGroup(*(Arrow((.35, y, 0), (4, y * .12, 0), buff=0, color="#FC775C")
                             for y in (-1, 1)))
        self.play(Create(refracted), moon.animate.set_color("#CB5845"), run_time=1.35)
        self.play(FadeIn(Text("RED LIGHT REACHES THE MOON", font_size=24, color="#FEB6A4")
                         .move_to((0, -2.75, 0))), run_time=.6)
        self.wait(1.1)


class SaturnRings(Scene):
    def construct(self):
        title = Text("ICE AND ROCK IN ORBIT", font_size=32).move_to((0, 3.15, 0))
        rings = VGroup(*(Ellipse(width=w, height=h, color=color)
                         for w, h, color in ((6, 1.45, "#95A9C4"), (8, 2, "#DBC39B"), (10, 2.7, "#95A9C4"))))
        saturn = Circle(1.12, color="#FFD68B", fill_color="#AE794E", fill_opacity=1)
        saturn.set_z_index(1)
        self.add(title, saturn)
        self.play(Create(rings), run_time=.55)
        grains = [Dot(ring.get_start(), radius=.10, color=color).set_z_index(2)
                  for ring, color in zip(rings, ("#E5F7FF", "#CBA688", "#DAF0FF"))]
        self.add(*grains)
        self.play(*(MoveAlongPath(grain, ring, rate_func=lambda t, speed=speed: (t * speed) % 1)
                    for ring, grain, speed in zip(rings, grains, (2, 1.55, 1.15))), run_time=2.7)
        self.wait(.6)


class WhiteDwarf(Scene):
    def construct(self):
        title = Text("FORCES INSIDE A WHITE DWARF", font_size=30).move_to((0, 3.1, 0))
        star = Circle(1.28, color="#D7E7FF", fill_color="#657EB0", fill_opacity=1)
        inward = VGroup()
        outward = VGroup()
        for angle in range(4):
            theta = (angle + .5) * math.pi / 2
            dx, dy = math.cos(theta), math.sin(theta)
            inward.add(Arrow((2.8 * dx, 2.8 * dy, 0), (1.5 * dx, 1.5 * dy, 0),
                             color="#FFA67A", buff=0))
            outward.add(Arrow((.25 * dx, .25 * dy, 0), (1.1 * dx, 1.1 * dy, 0),
                              color="#94E2F6", buff=0))
        self.add(title)
        self.play(Create(star), run_time=.65)
        self.play(Create(inward), FadeIn(Text("Gravity", color="#FFA67A", font_size=25)
                                         .move_to((-3, -2.65, 0))), run_time=.95)
        self.play(Create(outward), FadeIn(Text("Electron pressure", color="#94E2F6", font_size=25)
                                          .move_to((3, -2.65, 0))), run_time=1.05)
        self.wait(1.05)


DIAGRAMS = {"rainbow-split": RainbowDrop, "venus-spin": VenusPeriods, "mercury-day": MercuryDay,
            "eclipse-red": LunarEclipse, "saturn-rings": SaturnRings,
            "chandrasekhar-limit-hi": WhiteDwarf}


def render_diagram(topic_id, directory, fps):
    destination = directory / "science-diagram.mp4"
    with tempconfig({
        "renderer": "cairo", "pixel_width": 1080, "pixel_height": 608, "frame_rate": fps,
        "background_color": "#102C2A", "media_dir": str(directory / "manim"),
        "output_file": str(destination), "format": "mp4", "progress_bar": "none",
        "verbosity": "WARNING", "disable_caching": True,
    }):
        DIAGRAMS[topic_id]().render()
    return destination
