"""把 Pygame 2D 表面画到 OpenGL 窗口上（气泡/菜单）。"""
from __future__ import annotations

import pygame
from OpenGL.GL import (
    GL_BLEND, GL_CLAMP_TO_EDGE, GL_LINEAR, GL_MODELVIEW, GL_ONE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_PROJECTION, GL_QUADS, GL_RGBA, GL_SRC_ALPHA, GL_TEXTURE_2D,
    GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_WRAP_S,
    GL_TEXTURE_WRAP_T, GL_UNPACK_ALIGNMENT, GL_UNSIGNED_BYTE,
    glBegin, glBindTexture, glBlendFuncSeparate, glColor4f, glDisable, glEnable,
    glEnd, glGenTextures, glLoadIdentity, glMatrixMode, glOrtho, glPopMatrix,
    glPixelStorei, glPushMatrix, glTexCoord2f, glTexImage2D, glTexParameteri,
    glVertex2f, glViewport,
)


class SurfaceTexture:
    def __init__(self) -> None:
        self.tex = 0
        self.size = (0, 0)

    def upload(self, surface: pygame.Surface) -> None:
        w, h = surface.get_size()
        if self.tex == 0:
            self.tex = int(glGenTextures(1))
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        data = pygame.image.tostring(surface, "RGBA", True)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, data)
        self.size = (w, h)

    def draw(self, width: int, height: int) -> None:
        if not self.tex:
            return
        glMatrixMode(GL_PROJECTION)
        glPushMatrix()
        glLoadIdentity()
        glOrtho(0, width, height, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()
        glLoadIdentity()
        glEnable(GL_TEXTURE_2D)
        glEnable(GL_BLEND)
        # 帧缓冲是预乘 alpha：颜色用 straight-alpha 混合，alpha 必须按
        # “src + dst*(1-src)”累加，否则半透明边缘会 A² 变淡。
        glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
                            GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glColor4f(1, 1, 1, 1)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 1); glVertex2f(0, 0)
        glTexCoord2f(1, 1); glVertex2f(width, 0)
        glTexCoord2f(1, 0); glVertex2f(width, height)
        glTexCoord2f(0, 0); glVertex2f(0, height)
        glEnd()
        glDisable(GL_TEXTURE_2D)
        glPopMatrix()
        glMatrixMode(GL_PROJECTION)
        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)
        glViewport(0, 0, width, height)
