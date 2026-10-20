import { defineCollection } from "astro:content";
import { bookLoader } from "./content/loader.ts";
import { pageSchema } from "./content/metadata.ts";

export const collections = {
  maintainerRecords: defineCollection({
    loader: bookLoader(true),
    schema: pageSchema,
  }),
  pages: defineCollection({ loader: bookLoader(), schema: pageSchema }),
};
