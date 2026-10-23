import { defineCollection } from "astro:content";
import { docsLoader } from "./content/loader.ts";
import { pageSchema } from "./content/metadata.ts";

export const collections = {
  maintainerRecords: defineCollection({
    loader: docsLoader(true),
    schema: pageSchema,
  }),
  pages: defineCollection({ loader: docsLoader(), schema: pageSchema }),
};
