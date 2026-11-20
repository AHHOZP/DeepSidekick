from __future__ import annotations

import unittest

import pygame

from sidekick.desktop.menu import Item, Menu


class DesktopMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_click_returns_action(self):
        menu = Menu()
        menu.open((10, 10), [Item("退出", "quit")])
        surface = pygame.Surface((300, 300), pygame.SRCALPHA)
        menu.draw(surface)
        event = pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"button": 1,
                                     "pos": (menu.rect.x + 10,
                                             menu.rect.y + menu.PAD + 2)})
        self.assertEqual(menu.handle(event), ("quit", None))
        self.assertFalse(menu.active)

    def test_submenu_click_returns_child_action(self):
        menu = Menu()
        menu.open((10, 10), [
            Item("动作", "noop", children=[
                Item("挥手", "motion", "挥手")])])
        surface = pygame.Surface((500, 300), pygame.SRCALPHA)
        menu.draw(surface)
        menu.handle(pygame.event.Event(
            pygame.MOUSEMOTION,
            {"pos": (menu.rect.x + 10, menu.rect.y + menu.PAD + 2)}))
        menu.draw(surface)
        self.assertTrue(menu.sub_open)
        event = pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"button": 1,
                                     "pos": (menu.sub_rect.x + 10,
                                             menu.sub_rect.y + menu.PAD + 2)})
        self.assertEqual(menu.handle(event), ("motion", "挥手"))


if __name__ == "__main__":
    unittest.main()
