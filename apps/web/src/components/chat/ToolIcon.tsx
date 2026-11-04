import {
  BookSearch,
  Bot,
  CheckCheck,
  DatabaseZap,
  FileCode,
  FilePen,
  FileText,
  FolderTree,
  Globe,
  MessageCircleQuestion,
  Package,
  SquareTerminal,
  TableProperties,
  Wrench,
} from "lucide-react";

export function ToolIcon({ name, className }: { name: string; className?: string }) {
  switch (name) {
    case "shell":
      return <SquareTerminal className={className} aria-hidden />;
    case "run_python":
      return <FileCode className={className} aria-hidden />;
    case "install_packages":
      return <Package className={className} aria-hidden />;
    case "read_file":
      return <FileText className={className} aria-hidden />;
    case "write_file":
    case "edit_file":
      return <FilePen className={className} aria-hidden />;
    case "list_files":
      return <FolderTree className={className} aria-hidden />;
    case "http_request":
      return <Globe className={className} aria-hidden />;
    case "ask_user":
      return <MessageCircleQuestion className={className} aria-hidden />;
    case "submit_result":
      return <CheckCheck className={className} aria-hidden />;
    case "call_agent":
      return <Bot className={className} aria-hidden />;
    case "search_knowledge":
      return <BookSearch className={className} aria-hidden />;
    case "query_sql":
      return <DatabaseZap className={className} aria-hidden />;
    case "describe_sql_source":
      return <TableProperties className={className} aria-hidden />;
    default:
      return <Wrench className={className} aria-hidden />;
  }
}
