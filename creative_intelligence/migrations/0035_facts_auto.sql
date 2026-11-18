-- Feedback 2026-10-22: facts approve themselves (hub.needs_review). Facts already waiting are approved too, except
-- health claims, guarantees, shipping promises and anything that states a number, which still wait for a person.
UPDATE facts SET status='approved', note='auto-approved; switch to Do not use if it shouldn''t be used'
 WHERE status='pending' AND active=1 AND category NOT IN ('health', 'guarantee', 'shipping') AND text NOT GLOB '*[0-9]*';
