"""CPU-rendered science inserts for the Shorts renderer."""

from manim import Circle, Create, FadeIn, GrowFromEdge, LEFT, Line, Rectangle, Scene, Text, UP, VGroup, tempconfig


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


DIAGRAMS = {"rainbow-split": RainbowDrop, "venus-spin": VenusPeriods}


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
