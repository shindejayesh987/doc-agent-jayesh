"use client";

import { useEffect, useState, useCallback } from "react";
import { getCollectionDocuments, getCollections, deleteDocument } from "@/lib/api";
import { FileUploadArea } from "./FileUploadArea";
import type { Collection, Document } from "@/lib/types";
import {
  ArrowLeftIcon,
  CheckIcon,
  FileTextIcon,
  Loader2Icon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react";

interface CollectionViewProps {
  collectionId: string;
  activeDocIds: string[];
  onToggleDoc: (docId: string) => void;
  onBack: () => void;
  onDocumentsUploaded: () => void;
  userRole?: "admin" | "user";
}

export function CollectionView({
  collectionId,
  activeDocIds,
  onToggleDoc,
  onBack,
  onDocumentsUploaded,
  userRole = "user",
}: CollectionViewProps) {
  const [collection, setCollection] = useState<Collection | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchDocs = useCallback(async () => {
    try {
      const docs = await getCollectionDocuments(collectionId);
      setDocuments(docs);
    } catch (e) {
      console.error("Failed to load collection documents:", e);
    } finally {
      setLoading(false);
    }
  }, [collectionId]);

  useEffect(() => {
    // Load collection metadata
    getCollections().then((colls) => {
      const found = colls.find((c) => c.id === collectionId);
      if (found) setCollection(found);
    });
    fetchDocs();
  }, [collectionId, fetchDocs]);

  const handleUploadComplete = useCallback(() => {
    fetchDocs();
    onDocumentsUploaded();
  }, [fetchDocs, onDocumentsUploaded]);

  const handleDelete = async (docId: string) => {
    try {
      await deleteDocument(docId);
      setDocuments((prev) => prev.filter((d) => d.id !== docId));
    } catch (e) {
      console.error("Failed to delete document:", e);
    }
  };

  const indexed = documents.filter((d) => d.status === "indexed");
  const other = documents.filter((d) => d.status !== "indexed");

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      {/* Header */}
      <div className="mb-6">
        <button
          onClick={onBack}
          className="mb-4 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Back to Collections
        </button>

        {collection && (
          <div>
            <h2 className="text-xl font-semibold">{collection.name}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{collection.description}</p>
          </div>
        )}
      </div>

      {/* Upload section — global collections: admin only; user_uploads: everyone */}
      {(collectionId === "user_uploads" || userRole === "admin") && (
        <div className="mb-6">
          <FileUploadArea collectionId={collectionId} onUploadComplete={handleUploadComplete} />
        </div>
      )}

      {/* Document list */}
      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2Icon className="size-5 animate-spin text-muted-foreground" />
        </div>
      ) : indexed.length === 0 && other.length === 0 ? (
        <div className="rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
          No documents yet. Upload a PDF above to get started.
        </div>
      ) : (
        <div className="space-y-1">
          {indexed.length > 0 && (
            <p className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              {indexed.length} indexed document{indexed.length !== 1 ? "s" : ""}
              {activeDocIds.filter((id) => indexed.some((d) => d.id === id)).length > 0 &&
                ` · ${activeDocIds.filter((id) => indexed.some((d) => d.id === id)).length} selected`}
            </p>
          )}

          {indexed.map((doc) => {
            const isActive = activeDocIds.includes(doc.id);
            return (
              <div
                key={doc.id}
                className={`flex items-center gap-3 rounded-lg border px-4 py-3 transition-colors cursor-pointer ${
                  isActive
                    ? "border-primary/50 bg-primary/5"
                    : "hover:bg-muted/50"
                }`}
                onClick={() => onToggleDoc(doc.id)}
              >
                <div
                  className={`flex size-5 shrink-0 items-center justify-center rounded border transition-colors ${
                    isActive
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-muted-foreground/30"
                  }`}
                >
                  {isActive && <CheckIcon className="size-3.5" />}
                </div>

                <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />

                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{doc.name}</p>
                  <p className="text-[11px] text-muted-foreground">
                    {doc.page_count ?? "?"} pages
                    {doc.total_tokens ? ` · ${(doc.total_tokens / 1000).toFixed(0)}k tokens` : ""}
                  </p>
                </div>

                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(doc.id);
                  }}
                  className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100 [div:hover>&]:opacity-100"
                  title="Delete document"
                >
                  <Trash2Icon className="size-4" />
                </button>
              </div>
            );
          })}

          {other.map((doc) => (
            <div
              key={doc.id}
              className="flex items-center gap-3 rounded-lg border px-4 py-3 opacity-60"
            >
              {doc.status === "indexing" || doc.status === "uploaded" ? (
                <Loader2Icon className="size-4 shrink-0 animate-spin" />
              ) : (
                <XCircleIcon className="size-4 shrink-0 text-destructive" />
              )}
              <span className="truncate text-sm">{doc.name}</span>
              <span className="ml-auto text-[11px] text-muted-foreground">{doc.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
