-- 0009_failure_input_dim.sql
-- A checkpoint shape failure has two distinct dimensions: the feature
-- extractor's output (`embedding_dim`) and the classifier configuration it
-- rejected (`input_dim`). Without both, negative memory can mistake every
-- bad classifier dimension for the same exact failure and over-block plans.

ALTER TABLE failure_signatures ADD COLUMN input_dim INT;
