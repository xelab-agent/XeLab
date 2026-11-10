import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, unquote

from markdown_links import _markdown_links, _unescape_markdown, check_markdown_links, markdown_anchors


class MarkdownLinksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, content):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')
        return file

    def test_same_page_cross_page_encoded_and_named_anchors(self):
        other = self.write('other file.md', '''# 中文，标题！
## Repeat
## Repeat-1
## Repeat
<a name="manual-anchor"></a>
<a id="custom-ID"></a>
<span id="span-anchor"></span>
''')
        source = self.write('source.md', '''# My Section
[same](#my-section)
[encoded](other%20file.md#%E4%B8%AD%E6%96%87%E6%A0%87%E9%A2%98)
[first](other%20file.md#repeat)
[collision](other%20file.md#repeat-2)
[named](other%20file.md#manual-anchor)
[custom](other%20file.md#custom-ID)
[html](other%20file.md#span-anchor)
[top](#top)
''')
        edges, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [])
        self.assertIn(source.resolve(), edges)
        self.assertIn(other.resolve(), edges)

    def test_missing_anchors_and_missing_files_include_source_and_target(self):
        self.write('other.md', '# Present\n')
        source = self.write('source.md', '''# Present
[local](#absent)
[cross](other.md#absent)
[case-sensitive](other.md#Present)
[missing](missing.md#absent)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #absent',
            'broken Markdown anchor source.md -> other.md#absent',
            'broken Markdown anchor source.md -> other.md#Present',
            'broken Markdown link source.md -> missing.md#absent',
        ])

    def test_external_netloc_syntax_cannot_abort_local_link_validation(self):
        source = self.write('source.md', '''# Present
[external](https://[)
[protocol-relative](//[)
[missing](#absent)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #absent'])

    def test_heading_format_punctuation_unicode_and_duplicate_collision(self):
        anchors = markdown_anchors('''# This'll be a _Helpful_ Section About Θ!
## 中文，标点！
## Café &amp; `code.value`
## [Linked text](https://example.com)
## foo_bar
## \\_literal_
## 〇用例 ‿ Ⅳ Ⓐ
## ![](image.png) Tag
## Heading [ref][target]
## Heading [shortcut]
Setext title
============
## Repeat
## Repeat-1
## Repeat
## Repeat
## !!!
## !!!

[target]: #unused
[shortcut]: #unused
''')
        self.assertTrue({
            'thisll-be-a-helpful-section-about-θ', '中文标点', 'café--codevalue',
            'linked-text', 'foo_bar', '_literal_', '〇用例-‿-ⅳ-ⓐ', '-tag', 'heading-ref',
            'heading-shortcut', 'setext-title', 'repeat', 'repeat-1',
            'repeat-2', 'repeat-3', '-1',
        }.issubset(anchors), anchors)
        self.assertNotIn('repeat-4', anchors)

    def test_code_and_html_comments_do_not_create_links_or_anchors(self):
        source = self.write('source.md', '''# Visible
<!-- # Hidden\n<a name="hidden"></a> [bad](#absent) -->
[real](#visible)
`[inline](#absent)`
````markdown
# Fake
[inside](#absent)
<a name="fake-anchor"></a>
```
# Still fake
````
~~~text
# Also fake
[inside](missing.md)
~~~
<pre><a name="pre-anchor"></a></pre>

    [indented code](#absent)
    # Indented fake

<pre>
# HTML fake
[HTML code](#absent)
</pre>
<code>plain HTML code</code>
<!--
```
-->
## After comment
<pre>
~~~
</pre>
## After HTML
`<!--` [after](#after-comment)
```
code
```
    [indented after fence](#absent)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [])
        anchors = markdown_anchors(source.read_text(encoding='utf-8'))
        self.assertIn('visible', anchors)
        self.assertIn('after-comment', anchors)
        self.assertIn('after-html', anchors)
        self.assertTrue({'fake', 'still-fake', 'also-fake', 'fake-anchor',
                         'hidden', 'indented-fake', 'html-fake'}.isdisjoint(anchors))
        # HTML elements inside <pre> are still real anchors in GitHub's rendered HTML.
        self.assertIn('pre-anchor', anchors)

    def test_inline_titles_escapes_and_balanced_destinations(self):
        self.write('other(1).md', '# Section\n')
        source = self.write('source.md', r'''# Heading
[title](#heading "A title")
[angle](<#heading> 'A title')
[parentheses](other(1).md#section "A (title)")
\[literal](#not-a-link)
[not a link](#bad\ name)
![image](#not-a-link)
\![actual link](#missing)
[missing](#other-missing "title")
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #missing',
            'broken Markdown anchor source.md -> #other-missing',
        ])

    def test_reference_links_are_resolved_and_unused_definitions_are_not_checked(self):
        self.write('other.md', '# 中文 标题\n')
        source = self.write('source.md', '''# Local
[first][ target ]
[shortcut]
[collapsed][]
![image][bad-image]
[unresolved][unknown]
[broken][unknown]
[missing][broken]

[target]: other.md#%E4%B8%AD%E6%96%87-%E6%A0%87%E9%A2%98 "title"
[TARGET]: other.md#ignored-duplicate
[shortcut]: #local
[collapsed]: <#local> 'title'
[broken]: other.md#not-present
[unused]: missing.md#anything
[bad-image]: missing-image.md#anything
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> other.md#not-present'])

    def test_github_html_explicit_anchors_and_inline_code_links(self):
        source = self.write('source.md', '''<em id="em-anchor">text</em>
<code id="code-anchor">code</code>
<section id="section-anchor">section</section>
<details id="detail-anchor"><summary id="summary-anchor">summary</summary></details>
<pre id="pre-id"><a name="inside-pre"></a>
text
</pre>
<table><tr><td id="valid-cell">cell</td></tr></table>
<abbr id="stripped">text</abbr><td id="orphan-cell">cell</td>

[em](#em-anchor) [code](#code-anchor) [section](#section-anchor)
[detail](#detail-anchor) [summary](#summary-anchor)
[pre](#pre-id) [inside](#inside-pre)
[table](#valid-cell) [stripped](#stripped) [orphan](#orphan-cell)
<span title="[not a link](#wrong)">text</span>
<code>[actual link](#missing)</code>
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #stripped',
            'broken Markdown anchor source.md -> #orphan-cell',
            'broken Markdown anchor source.md -> #missing',
        ])

    def test_raw_html_blocks_do_not_parse_markdown_links(self):
        source = self.write('source.md', '''# Present
<div>
[div](#missing-div)
</div>

<table>
<tr><td>[table](#missing-table)</td></tr>
</table>

<details>
<summary>x</summary>
[details](#missing-details)
</details>

<div>[block-inline](#missing-block-inline)</div>

<span>[inline](#missing-inline)</span>

[real](#missing-real)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #missing-inline',
            'broken Markdown anchor source.md -> #missing-real',
        ])

    def test_headings_in_raw_html_blocks_do_not_create_anchors(self):
        source = self.write('source.md', '''<div>
# fake
</div>

# real
[fake](#fake) [real](#real)
''')
        anchors = markdown_anchors(source.read_text(encoding='utf-8'))
        self.assertIn('real', anchors)
        self.assertNotIn('fake', anchors)
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #fake'])

    def test_indented_list_link_is_still_checked(self):
        source = self.write('source.md', '''# Existing
- list item

    [nested link](#absent)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #absent'])

    def test_inline_code_delimiter_escapes_and_heading_spaces(self):
        source = self.write('source.md', r'''# Before ` foo ` After
\\`[not a link](#missing)`
\`[actual link](#missing)`
[heading](#before-foo-after)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #missing'])

    def test_blockquote_code_headings_and_reference_definitions(self):
        source = self.write('source.md', '''# Present

> ```markdown
> [inside fence](#absent)
> ```
>
>     [inside indented code](#absent)
>
> ## Quoted
> ## Present
>
> Setext heading
> --------------
>
> [quoted-ref]: #quoted

> > ~~~
> > [inside nested fence](#absent)
> > ~~~

[quoted][quoted-ref]
[duplicate](#present-1) [setext](#setext-heading)
> ```
> still in quoted code
[outside](#missing)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #missing'])
        self.assertTrue({'quoted', 'present-1', 'setext-heading'}.issubset(
            markdown_anchors(source.read_text(encoding='utf-8'))))

    def test_repository_root_links_and_reject_paths_outside_repository(self):
        repo = self.root / 'repo'
        (repo / 'docs').mkdir(parents=True)
        (repo / 'README.md').write_text('# Home\n', encoding='utf-8')
        outside = self.root / 'private.md'
        outside.write_text('# Private\n', encoding='utf-8')
        (repo / 'docs' / 'external.md').symlink_to(outside)
        source = repo / 'docs' / 'source.md'
        source.write_text('''# Present
[root](/docs/source.md#present)
[readme](/README.md#home)
[traversal](../../private.md#private)
[absolute traversal](/../private.md#private)
[symlink](external.md#private)
''', encoding='utf-8')
        _, errors = check_markdown_links(source, repo)
        self.assertEqual(errors, [
            'broken Markdown link docs/source.md -> ../../private.md#private (outside repository)',
            'broken Markdown link docs/source.md -> /../private.md#private (outside repository)',
            'broken Markdown link docs/source.md -> external.md#private (outside repository)',
        ])

    def test_unclosed_quoted_html_block_does_not_hide_following_links(self):
        source = self.write('source.md', '''> <pre>
> [not a link](#absent)
<a id="after-pre"></a>
[existing](#after-pre) [missing](#missing)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #missing'])

    def test_lazy_quote_continuations_are_not_indented_code(self):
        source = self.write('source.md', '''> paragraph
    [paragraph continuation](#missing-paragraph)

> - list item
    [list continuation](#missing-list)

> <em>paragraph</em>
    [inline HTML continuation](#missing-html)

> ## heading
    [indented code](#ignored)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #missing-paragraph',
            'broken Markdown anchor source.md -> #missing-list',
            'broken Markdown anchor source.md -> #missing-html',
        ])

    def test_multiline_code_and_fences_in_nested_containers(self):
        source = self.write('source.md', '''# Present

`prefix
[fake](#absent)
suffix`
[real](#missing-multiline)

> `prefix
> [fake](#absent)
> suffix`
[real](#missing-quoted)

- > ```
  > [fake](#absent)
  > ```
[real](#missing-list-quote)

1. > ```
   > [fake](#absent)
   > ```
[real](#missing-numbered)

- ```
  [fake](#absent)
  ```
[real](#missing-list)

> - ```
>   [fake](#absent)
>   ```
[real](#missing-quote-list)

paragraph
    ```
    [real](#missing-paragraph)

`unclosed
## Heading
[real](#missing-heading)`
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #missing-multiline',
            'broken Markdown anchor source.md -> #missing-quoted',
            'broken Markdown anchor source.md -> #missing-list-quote',
            'broken Markdown anchor source.md -> #missing-numbered',
            'broken Markdown anchor source.md -> #missing-list',
            'broken Markdown anchor source.md -> #missing-quote-list',
            'broken Markdown anchor source.md -> #missing-paragraph',
            'broken Markdown anchor source.md -> #missing-heading',
        ])

    def test_html_anchor_inside_multiline_code_is_not_real(self):
        source = self.write('source.md', '''`prefix
<a id="hidden"></a>
suffix`

[missing](#hidden)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #hidden'])

    def test_html_fragments_in_multiline_code_do_not_hide_later_links(self):
        cases = [
            ('`prefix\nexample <pre> text\nsuffix`\n\n[real](#missing)', '#missing'),
            ('`prefix\ntext <!-- not closed\nsuffix`\n\n[real](#missing)', '#missing'),
            ('> `prefix\n> example <pre> text\n> suffix` [real](#missing)', '#missing'),
            ('- `prefix\n  example <pre> text\n  suffix` [real](#missing)', '#missing'),
        ]
        for content, target in cases:
            with self.subTest(content=content):
                source = self.write('source.md', content)
                _, errors = check_markdown_links(source, self.root)
                self.assertEqual(errors, [f'broken Markdown anchor source.md -> {target}'])

    def test_multiline_backticks_inside_html_pre_preserve_real_anchors(self):
        source = self.write('source.md', '''<pre>
`literal
<a id="inside-pre-code"></a>
suffix`
</pre>

[valid](#inside-pre-code)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [])

    def test_reference_definitions_do_not_interrupt_paragraphs_or_code(self):
        source = self.write('source.md', '''`prefix
[fake-code]: #absent-code
suffix`

[not a link][fake-code]

paragraph
[fake-paragraph]: #absent-paragraph

[not a link][fake-paragraph]

> paragraph
> [fake-quote]: #absent-quote

[not a link][fake-quote]

# Heading
[allowed]: #missing-allowed

[valid reference][allowed]

> # Heading
> [quoted-allowed]: #missing-quoted

[valid reference][quoted-allowed]
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [
            'broken Markdown anchor source.md -> #missing-allowed',
            'broken Markdown anchor source.md -> #missing-quoted',
        ])

    def test_external_images_and_non_markdown_fragments_remain_out_of_scope(self):
        self.write('schema.json', '{}')
        source = self.write('source.md', '''# Valid
[remote](https://example.com/missing.md#missing)
[remote-network](//example.com/missing.md#missing)
![image](missing.svg#missing)
[json](schema.json#/missing/pointer)
[local](#valid)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, [])

    def test_explicit_anchor_names_are_case_sensitive_and_do_not_consume_heading_slug(self):
        source = self.write('source.md', '''<a name="repeat"></a>
## Repeat
## Repeat
[heading](#repeat-1)
[named](#repeat)
[wrong-case](#REPEAT)
''')
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> #REPEAT'])

    def test_percent_encoded_fragment_is_decoded_once(self):
        self.write('other.md', '# 中文 标题\n<a name="literal%20value"></a>\n')
        source = self.write('source.md', '\n'.join([
            f'[unicode](other.md#{quote("中文-标题")})',
            '[literal-percent](other.md#literal%2520value)',
            '[decoded](other.md#literal%20value)',
        ]))
        _, errors = check_markdown_links(source, self.root)
        self.assertEqual(errors, ['broken Markdown anchor source.md -> other.md#literal%20value'])

    def test_links_match_github_rendered_offline_fixtures(self):
        for name in ('github-markdown-links.json', 'github-blockquote-links.json',
                     'github-code-boundaries.json'):
            fixture = Path(__file__).parent / 'fixtures' / name
            cases = json.loads(fixture.read_text(encoding='utf-8'))['cases']
            self.assertTrue(cases)
            for number, case in enumerate(cases):
                with self.subTest(fixture=name, case=number):
                    actual = [unquote(_unescape_markdown(item)) for item in _markdown_links(case['markdown'])]
                    self.assertEqual(actual, case['destinations'])

    def test_headings_match_rendered_text_and_slugger_offline_fixtures(self):
        fixture = Path(__file__).parent / 'fixtures/github-heading-slugs.json'
        cases = json.loads(fixture.read_text(encoding='utf-8'))['cases']
        self.assertTrue(cases)
        for number, case in enumerate(cases):
            with self.subTest(case=number):
                self.assertEqual(markdown_anchors(case['markdown']) - {'top'}, {case['anchor']})


if __name__ == '__main__':
    unittest.main()
