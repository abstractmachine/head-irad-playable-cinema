# Inspector Grid Contract

This contract defines the shared inspector layout system for all visualizers.

## Core Rules

1. Edge-to-edge section bodies
- Collapsible section body content should align to the section grid edges.
- Avoid extra nested wrapper margins unless intentionally required.

2. Shared spacing token
- Use `theme.SECTION_GAP` for inspector panel margins and section spacing.
- Do not hardcode ad-hoc spacing in inspector composition.

3. Shared control rhythm
- Use `theme.BTN_H` as the canonical row/control height for inspector actions.
- Keep related controls in a row at consistent heights.

4. Shared color/interaction states
- Use shared button styles and accent tokens:
  - normal: `theme.BTN_BG` / `theme.TEXT`
  - hover/selected: `theme.ACCENT` / `theme.ACCENT_TEXT`

5. Overflow behavior
- Collapsed sections should top-snap and not consume extra vertical space.
- Expanded content can grow; scrolling should be handled by the intended scroll container.

## Implementation Notes

- Preferred section primitive: `visualizers/components/collapsible_section.py`.
- Preferred metadata table primitive: `visualizers/components/metadata_block.py`.
- For icon readability on hover/selected states, prefer shared hover icon helpers in `visualizers/components/hover_icon_button.py`.

## Quick Checklist for New Inspectors

- [ ] Panel margins/spacing use `theme.SECTION_GAP`
- [ ] Section body does not add accidental extra inset
- [ ] Row controls use `theme.BTN_H`
- [ ] Hover and selected states use accent tokens
- [ ] Open/closed section behavior preserves top alignment and expected scrolling
