import pygame

def handle_event(
    event,
    on_random=None,
    on_prev=None,
    on_next=None,
    on_time_random=None,
    on_gremlin_toggle=None,
    on_gremlin_speed=None,
) -> bool:
    """
    Handle a single pygame event.
    - Returns True if the app should quit.
    - R: random film (random time)
    - PageUp/PageDown: previous/next (start at 0)
    - T: randomize time for current film
    - G: toggle gremlin
    - 1..9: set gremlin speed to N seconds
    """
    if event.type == pygame.QUIT:
        return True

    if event.type == pygame.KEYDOWN:
        if event.key in (pygame.K_ESCAPE, pygame.K_q):
            return True
        elif event.key == pygame.K_r and on_random:
            on_random()
        elif event.key == pygame.K_PAGEUP and on_prev:
            on_prev()
        elif event.key == pygame.K_PAGEDOWN and on_next:
            on_next()
        elif event.key == pygame.K_t and on_time_random:
            on_time_random()
        elif event.key == pygame.K_g and on_gremlin_toggle:
            on_gremlin_toggle()
        elif pygame.K_1 <= event.key <= pygame.K_9 and on_gremlin_speed:
            seconds = event.key - pygame.K_0  # 1..9
            on_gremlin_speed(seconds)

    return False