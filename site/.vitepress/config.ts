import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

// Deployed as a GitHub Pages *project* site at https://whanyu1212.github.io/Wisp/,
// so every absolute asset URL needs the repo name as a base path. Override with
// DOCS_BASE=/ when serving from a custom domain or the repository root.
const base = process.env.DOCS_BASE ?? '/Wisp/'

export default withMermaid(
  defineConfig({
    title: 'Wisp',
    description:
      'A coding agent that stays in sync with you — steer it mid-run, approve what it changes, ' +
      'and inspect everything it did.',
    base,
    lang: 'en-US',
    cleanUrls: true,

    // Fail the build on a broken internal link rather than shipping one.
    ignoreDeadLinks: false,

    // Asset URLs in `head` are not base-prefixed automatically, unlike ones in
    // Markdown or themeConfig.logo — so these are written with `base` applied.
    head: [
      ['link', { rel: 'icon', type: 'image/x-icon', href: `${base}favicon.ico` }],
      ['link', { rel: 'apple-touch-icon', href: `${base}apple-touch-icon.png` }],
      ['meta', { name: 'theme-color', content: '#5b7fff' }],
      ['meta', { property: 'og:type', content: 'website' }],
      ['meta', { property: 'og:title', content: 'Wisp' }],
      [
        'meta',
        {
          property: 'og:description',
          content:
            'A coding agent that stays in sync with you — steer it mid-run, approve what it ' +
            'changes, and inspect everything it did.',
        },
      ],
      [
        'meta',
        {
          property: 'og:image',
          content: 'https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner-v2.png',
        },
      ],
      ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
    ],

    themeConfig: {
      siteTitle: 'Wisp',
      logo: '/logo.png',

      nav: [
        { text: 'Guide', link: '/guide/', activeMatch: '/guide/' },
        { text: 'Reference', link: '/reference/', activeMatch: '/reference/' },
        { text: 'Architecture', link: '/architecture/', activeMatch: '/architecture/' },
        { text: 'Contributing', link: '/contributing/', activeMatch: '/contributing/' },
        {
          text: 'v0.1.0rc1',
          items: [
            { text: 'Changelog', link: 'https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md' },
            { text: 'PyPI', link: 'https://pypi.org/project/wisp-ai/' },
          ],
        },
      ],

      sidebar: {
        '/guide/': [
          {
            text: 'Getting started',
            items: [
              { text: 'Introduction', link: '/guide/' },
              { text: 'Installation', link: '/guide/installation' },
              { text: 'Quickstart', link: '/guide/quickstart' },
            ],
          },
          {
            text: 'Core concept',
            items: [{ text: 'Staying in sync', link: '/guide/staying-in-sync' }],
          },
          {
            text: 'Using Wisp',
            items: [
              { text: 'Interfaces', link: '/guide/interfaces' },
              { text: 'Providers & auth', link: '/guide/providers' },
              { text: 'Tools & safety', link: '/guide/tools-and-safety' },
              { text: 'Sessions', link: '/guide/sessions' },
              { text: 'Context & compaction', link: '/guide/context-and-compaction' },
              { text: 'Agent skills', link: '/guide/skills' },
              { text: 'TUI', link: '/guide/tui' },
            ],
          },
        ],

        '/reference/': [
          {
            text: 'Reference',
            items: [
              { text: 'Overview', link: '/reference/' },
              { text: 'CLI', link: '/reference/cli' },
              { text: 'Configuration', link: '/reference/configuration' },
              { text: 'Environment variables', link: '/reference/environment' },
              { text: 'Events', link: '/reference/events' },
              { text: 'RPC protocol', link: '/reference/rpc' },
              { text: 'SDK', link: '/reference/sdk' },
            ],
          },
        ],

        '/architecture/': [
          {
            text: 'Architecture',
            items: [
              { text: 'Overview', link: '/architecture/' },
              { text: 'Layer stack', link: '/architecture/layers' },
              { text: 'Event model', link: '/architecture/events' },
              { text: 'Safety model', link: '/architecture/safety' },
              { text: 'Extensions', link: '/architecture/extensions' },
              { text: 'TUI internals', link: '/architecture/tui' },
            ],
          },
        ],

        '/contributing/': [
          {
            text: 'Contributing',
            items: [
              { text: 'Overview', link: '/contributing/' },
              { text: 'Development setup', link: '/contributing/development' },
              { text: 'Testing', link: '/contributing/testing' },
              { text: 'Releasing', link: '/contributing/releasing' },
            ],
          },
        ],
      },

      socialLinks: [{ icon: 'github', link: 'https://github.com/whanyu1212/Wisp' }],

      search: {
        provider: 'local',
      },

      editLink: {
        pattern: 'https://github.com/whanyu1212/Wisp/edit/main/site/:path',
        text: 'Edit this page on GitHub',
      },

      outline: { level: [2, 3] },

      footer: {
        message: 'Released under the MIT License.',
        copyright: 'Copyright © 2025-present Wisp contributors',
      },
    },

    markdown: {
      lineNumbers: false,
    },
  })
)
