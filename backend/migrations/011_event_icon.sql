-- One icon per event, chosen by the adapter.
--
-- The router used to prepend an icon derived from severity while the adapter
-- put a stage icon at the front of the title, so an event whose stage was not
-- in the icon map rendered as "• • Our run ...". Severity decides whether the
-- phone buzzes; it should not also decide the glyph, because the stage is the
-- more informative thing to show.
ALTER TABLE comp_event ADD COLUMN IF NOT EXISTS icon text NOT NULL DEFAULT '';
