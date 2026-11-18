-- The owner may let a persona tell first-person memories (2026-11-04: "do allow the grandma to have fake memories").
-- The claims check then lets the memory itself through; a fact inside it still has to be approved.
ALTER TABLE presenters ADD COLUMN memories INTEGER NOT NULL DEFAULT 0;
