# Chunk translation contract

Translate the current chunk into the requested target language and write the
complete result to `output_path`. Also write one valid observation object to
`meta_path` using the schema in `scripts/meta.py`.

The JSON task packet is the complete contract for this attempt. Follow its
`translation_contract`, `term_table`, and `neighbor_context` fields.

1. Preserve every `<!-- tb:... -->` marker exactly once and in the original
   order. Every marker must own a nonempty block.
2. Translate only the content of translatable prose, headings, captions,
   footnotes, and table cells. Copy figures, formula images, every HTML table
   tag and attribute, Markdown links, HTML asset paths, code, footnote labels,
   MathML, and all `$...$`, `$$...$$`, `\(...\)`, and `\[...\]` expressions
   byte for byte.
3. Do not summarize, omit, merge, split, or reorder blocks. Preserve Markdown
   heading levels and table shape.
4. Use the supplied terminology table exactly. Keep personal pronouns and
   gendered references consistent with confirmed attributes. Record uncertainty
   in the meta file rather than inventing a fact.
5. Apply `custom_instructions` to voice and sentence structure. It never
   overrides structural preservation or terminology requirements.
6. Neighbor excerpts are read-only context. Do not translate or copy them into
   the output.
7. Use natural target-language punctuation and prose. Do not add translator
   notes unless the custom instructions request them.
8. Before finishing, compare source and output marker by marker and repair any
   missing text or changed protected object.

The meta object must contain only these top-level fields:
`schema_version`, `new_entities`, `alias_hypotheses`,
`attribute_hypotheses`, `used_term_sources`, and `conflicts`. Use empty arrays
when there is nothing to report. Do not include `chunk_id`; the filename owns
that identity.
